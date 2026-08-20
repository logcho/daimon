"""Graph behavior with fake models: the tool loop, error handling, the
recursion cap actually binding, checkpointer session continuity, pro-vs-flash
routing, and the run_turn event sequence (thinking bracketing, label reuse,
done/error exclusivity)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from daimon_agent.graph import (
    build_graph,
    close_checkpointer,
    extract_result,
    make_sqlite_checkpointer,
    run_config,
)
from daimon_agent.run import run_turn

from fakes import FakeRouter, FakeTool, invalid_tool_call, tool_call


async def test_tool_once_then_answer(settings) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call("fake", {"query": "x"}, id="call-1"),
            AIMessage(content="The answer is 42."),
        ]
    )
    tool = FakeTool(name="fake", responses=["ok"])
    graph = build_graph(settings, router, [tool])

    state = await graph.ainvoke({"messages": [HumanMessage(content="hello")]}, run_config("t1"))

    assert tool.calls == [{"query": "x"}]
    messages = state["messages"]
    assert any(isinstance(m, ToolMessage) and m.content == "ok" for m in messages)
    assert any(isinstance(m, AIMessage) and m.content == "The answer is 42." for m in messages)
    assert extract_result(messages) == "The answer is 42."


async def test_tool_error_becomes_error_tool_message(settings) -> None:
    router = FakeRouter(
        pro_script=[tool_call("fake", {}), AIMessage(content="recovered from the failure")]
    )
    tool = FakeTool(name="fake", responses=[])  # dry script → raises
    graph = build_graph(settings, router, [tool])

    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("t2"))

    messages = state["messages"]
    error = next(m for m in messages if isinstance(m, ToolMessage) and m.status == "error")
    assert "fake tool exploded" in error.content


async def test_recursion_cap_binds(settings) -> None:
    # The model never answers — every call invokes the tool again. With a
    # tight recursion_limit the graph must raise instead of spinning forever.
    router = FakeRouter(pro_script=[tool_call("fake", {}, id=f"call-{i}") for i in range(10)])
    tool = FakeTool(name="fake", responses=["ok"] * 10)
    graph = build_graph(settings, router, [tool])

    config = {"configurable": {"thread_id": "t3"}, "recursion_limit": 4}
    with pytest.raises(GraphRecursionError):
        await graph.ainvoke({"messages": [HumanMessage(content="loop forever")]}, config)


async def test_checkpointer_busy_timeout(settings) -> None:
    # Concurrent turns against the same workspace's checkpoints file (e.g.
    # two named sessions) may still race — the pragma value IS the contract:
    # wait, don't error.
    checkpointer = await make_sqlite_checkpointer(settings.resolved_checkpoints_db)
    try:
        cursor = await checkpointer.conn.execute("PRAGMA busy_timeout")
        assert (await cursor.fetchone())[0] == 10000
    finally:
        await close_checkpointer(checkpointer)


async def test_two_turn_session_continuity_via_checkpointer(settings, tmp_path) -> None:
    checkpointer = await make_sqlite_checkpointer(tmp_path / "checkpoints.db")
    router = FakeRouter(
        pro_script=[
            tool_call("fake", {"note": "first"}, id="call-1"),
            AIMessage(content="done first"),
            AIMessage(content="done second"),
        ]
    )
    tool = FakeTool(name="fake", responses=["ok"])
    graph = build_graph(settings, router, [tool], checkpointer=checkpointer)

    try:
        await graph.ainvoke({"messages": [HumanMessage(content="turn one")]}, run_config("sess"))
        await graph.ainvoke({"messages": [HumanMessage(content="turn two")]}, run_config("sess"))

        # The second turn's model call (the last one) saw the full first-turn
        # history — both the question and the finished answer.
        second_input = router._pro.calls[-1]
        texts = [str(getattr(m, "content", "")) for m in second_input]
        assert any("turn one" in t for t in texts)
        assert any("done first" in t for t in texts)

        messages = (await graph.aget_state(run_config("sess"))).values["messages"]
        assert extract_result(messages) == "done second"
    finally:
        await close_checkpointer(checkpointer)


async def test_agent_node_routes_to_pro_model_not_flash(settings) -> None:
    router = FakeRouter(pro_script=[AIMessage(content="plain answer")])
    graph = build_graph(settings, router, [])

    await graph.ainvoke({"messages": [HumanMessage(content="hi")]}, run_config("t5"))

    assert len(router._pro.calls) == 1
    assert router._flash.calls == []


def test_extract_result_last_non_blank_ai_text_wins() -> None:
    messages = [
        HumanMessage(content="q"),
        AIMessage(content="interim"),
        ToolMessage(content="result", tool_call_id="c1"),
        AIMessage(content=""),
        AIMessage(content="   \n  "),
        AIMessage(content="final answer"),
    ]
    assert extract_result(messages) == "final answer"


def test_extract_result_falls_back_when_all_blank() -> None:
    assert extract_result([HumanMessage(content="q"), AIMessage(content="   ")]) == "Task complete."


async def test_run_turn_emits_bracketed_sequence(settings, tmp_path) -> None:
    # run_turn reads the post-turn state back via aget_state, so it needs a
    # checkpointer-backed graph — exactly how the CLI and server run it.
    checkpointer = await make_sqlite_checkpointer(tmp_path / "cp.db")
    router = FakeRouter(
        pro_script=[
            tool_call("fake", {"arg": 1}, id="call-fake-1"),
            AIMessage(content="final answer"),
        ]
    )
    tool = FakeTool(name="fake", responses=["ok"])
    graph = build_graph(settings, router, [tool], checkpointer=checkpointer)
    events: list[dict] = []

    try:
        result = await run_turn("do it", "seq", settings, events.append, graph=graph)
    finally:
        await close_checkpointer(checkpointer)

    assert result == "final answer"
    # step(Thinking, running) opens; it carries no `tool` key.
    assert events[0] == {
        "type": "step",
        "id": events[0]["id"],
        "label": "Thinking",
        "status": "running",
    }
    assert "tool" not in events[0]
    # Per-tool lifecycle: running then done, label/tool/id reused (run.ts invariant).
    running = [e for e in events if e.get("status") == "running" and e.get("label") == "fake"]
    done = [e for e in events if e.get("status") == "done" and e.get("label") == "fake"]
    assert len(running) == len(done) == 1
    assert running[0]["id"] == done[0]["id"]
    assert running[0]["tool"] == done[0]["tool"] == "fake"
    # Thinking, done closes the bracket; done terminates (exclusive with error).
    assert events[-2] == {
        "type": "step",
        "id": events[0]["id"],
        "label": "Thinking",
        "status": "done",
    }
    # `done` carries the turn's usage totals alongside the result. The scripted
    # model reports no usage, so the counters are zero — the key's presence is
    # the contract, not the numbers.
    assert events[-1]["type"] == "done"
    assert events[-1]["result"] == "final answer"
    assert events[-1]["usage"]["input_tokens"] == 0


async def test_run_turn_error_is_exclusive(settings, tmp_path) -> None:
    checkpointer = await make_sqlite_checkpointer(tmp_path / "cp.db")
    router = FakeRouter(pro_script=[])
    router._pro.raise_on_call = True
    graph = build_graph(settings, router, [], checkpointer=checkpointer)
    events: list[dict] = []

    try:
        result = await run_turn("boom", "err", settings, events.append, graph=graph)
    finally:
        await close_checkpointer(checkpointer)

    assert result is None
    assert all(e["type"] != "done" for e in events)
    assert events[-1]["type"] == "error"
    assert "model exploded" in events[-1]["message"]


# --- malformed tool calls -----------------------------------------------------

async def test_invalid_tool_call_is_answered_not_ignored(settings) -> None:
    """A call whose arguments didn't parse lands in `invalid_tool_calls`, not
    `tool_calls`. The turn used to end right there, leaving it unanswered in
    the stored history — and the API counts it as an open call, so every later
    request in that session was rejected."""
    router = FakeRouter(
        pro_script=[
            invalid_tool_call("fake", "abc", id="call-bad"),
            AIMessage(content="retried with valid arguments"),
        ]
    )
    tool = FakeTool(name="fake", responses=["never reached"])
    graph = build_graph(settings, router, [tool])

    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("bad-1"))

    answer = next(
        m for m in state["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "call-bad"
    )
    assert answer.status == "error"
    assert "did not parse" in answer.content
    assert tool.calls == []  # nothing executed — there were no arguments to execute
    assert extract_result(state["messages"]) == "retried with valid arguments"


def test_sanitize_repairs_a_stored_malformed_call() -> None:
    """The repair path for a history that already contains one — a session
    poisoned before this fix must become usable again, not stay broken."""
    from daimon_agent.graph import sanitize_messages

    poisoned = [
        HumanMessage(content="go"),
        invalid_tool_call("fake", "abc", id="call-bad"),
        AIMessage(content="carried on regardless"),
    ]

    repaired = sanitize_messages(poisoned)

    answered = [m for m in repaired if isinstance(m, ToolMessage) and m.tool_call_id == "call-bad"]
    assert len(answered) == 1
    assert repaired.index(answered[0]) == 2  # right after the message that opened it


# --- transient model failures -------------------------------------------------

async def test_stream_retries_a_mid_stream_failure(settings, monkeypatch) -> None:
    """The provider client's own max_retries only covers establishing the
    request. A stream that dies after the first chunk used to propagate out and
    end the turn, discarding every step of work that came before it."""
    import daimon_agent.graph as graph_module

    monkeypatch.setattr(graph_module, "STREAM_BACKOFF_S", (0.0, 0.0))
    router = FakeRouter(pro_script=[AIMessage(content="survived the reset")])
    router._pro.raise_mid_stream = 2  # fail twice, succeed on the third attempt
    graph = build_graph(settings, router, [])

    events: list[dict] = []
    from daimon_agent.emitter import set_active_emit

    set_active_emit(events.append)
    try:
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content="go")]}, run_config("retry-1")
        )
    finally:
        set_active_emit(None)

    assert extract_result(state["messages"]) == "survived the reset"
    # A silent retry is indistinguishable from a hang, and the turn watchdog
    # is watching for exactly that silence.
    retries = [e for e in events if e.get("type") == "retry"]
    assert [e["attempt"] for e in retries] == [2, 3]
    assert "ConnectionError" in retries[0]["reason"]


async def test_stream_does_not_retry_a_permanent_failure(settings) -> None:
    """A 400 will fail identically forever; retrying it just costs three times
    as long before the same error reaches the user."""
    from daimon_agent.graph import stream_model

    class Rejecting:
        calls = 0

        async def astream(self, messages):
            Rejecting.calls += 1
            raise ValueError("invalid request")
            yield  # pragma: no cover — makes this an async generator

    with pytest.raises(ValueError):
        await stream_model(Rejecting(), [HumanMessage(content="go")])
    assert Rejecting.calls == 1


def test_transient_classification_reads_the_status_first() -> None:
    from daimon_agent.graph import _is_transient

    class WithStatus(Exception):
        def __init__(self, status: int) -> None:
            self.status_code = status

    assert _is_transient(WithStatus(429)) is True
    assert _is_transient(WithStatus(503)) is True
    assert _is_transient(WithStatus(400)) is False
    assert _is_transient(WithStatus(404)) is False
    # No status to go on — fall back to the exception family.
    assert _is_transient(ConnectionError("reset")) is True
    assert _is_transient(TimeoutError("slow")) is True
    assert _is_transient(ValueError("bad argument")) is False


# --- concurrent tool execution ------------------------------------------------

async def test_read_only_calls_in_one_message_run_concurrently(settings) -> None:
    """Four reads used to cost four round trips of latency: the tools node was
    a plain for-loop with an await inside it."""
    import time

    args = [{"file_path": f"{i}.py"} for i in range(4)]
    router = FakeRouter(
        pro_script=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": a, "id": f"r{i}", "type": "tool_call"}
                    for i, a in enumerate(args)
                ],
            ),
            AIMessage(content="read them all"),
        ]
    )
    read = FakeTool(name="read_file", responses=[f"body {i}" for i in range(4)], delay=0.1)
    graph = build_graph(settings, router, [read])

    started = time.monotonic()
    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("par-1"))
    elapsed = time.monotonic() - started

    assert elapsed < 0.3  # sequential would be 0.4s of sleeping alone
    answers = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    # Order follows the model's tool_calls, not completion order.
    assert [m.tool_call_id for m in answers] == ["r0", "r1", "r2", "r3"]
    assert [m.content for m in answers] == ["body 0", "body 1", "body 2", "body 3"]


async def test_a_write_between_reads_still_happens_in_order(settings) -> None:
    """The batching is per *run* of consecutive safe calls. A read scheduled
    before a write must never observe that write."""
    order: list[str] = []

    class Recording(FakeTool):
        async def _arun(self, **kwargs):
            order.append(self.name)
            return await super()._arun(**kwargs)

    router = FakeRouter(
        pro_script=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"file_path": "a"}, "id": "r1", "type": "tool_call"},
                    {"name": "write_file", "args": {"file_path": "a", "content": "x"},
                     "id": "w1", "type": "tool_call"},
                    {"name": "read_file", "args": {"file_path": "b"}, "id": "r2", "type": "tool_call"},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    read = Recording(name="read_file", responses=["before", "after"], delay=0.05)
    write = Recording(name="write_file", responses=["wrote"])
    graph = build_graph(settings, router, [read, write])

    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("ord-1"))

    assert order == ["read_file", "write_file", "read_file"]
    answers = {m.tool_call_id: m.content for m in state["messages"] if isinstance(m, ToolMessage)}
    assert answers["r1"] == "before" and answers["r2"] == "after"


async def test_a_failure_inside_a_batch_does_not_take_the_batch_down(settings) -> None:
    router = FakeRouter(
        pro_script=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"file_path": "a"}, "id": "r1", "type": "tool_call"},
                    {"name": "read_file", "args": {"file_path": "b"}, "id": "r2", "type": "tool_call"},
                ],
            ),
            AIMessage(content="handled it"),
        ]
    )
    read = FakeTool(name="read_file", responses=["only one response"])  # second call raises
    graph = build_graph(settings, router, [read])

    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("batch-err"))

    answers = {m.tool_call_id: m for m in state["messages"] if isinstance(m, ToolMessage)}
    assert answers["r1"].content == "only one response"
    assert answers["r2"].status == "error"
    assert "fake tool exploded" in answers["r2"].content


# --- the result ceiling -------------------------------------------------------

def test_clip_result_leaves_a_result_that_fits_alone() -> None:
    from daimon_agent.graph import clip_result

    assert clip_result("short", limit=100) == "short"


def test_clip_result_says_what_was_cut_and_what_to_do() -> None:
    """A result that simply stops invites the model to run the same call again
    and hope."""
    from daimon_agent.graph import clip_result

    out = clip_result("x" * 500, limit=100)

    assert out.startswith("x" * 100)
    assert "400 more characters" in out
    assert "Narrow the request" in out


async def test_an_oversized_tool_result_is_clipped_before_it_reaches_the_model(settings) -> None:
    """Several tools had no limit of their own at all — a note, a skill, a
    registry search. Those are the ones that surprise you."""
    from daimon_agent.graph import MAX_TOOL_RESULT_CHARS

    router = FakeRouter(
        pro_script=[tool_call("read_note", {"name": "huge"}, id="n1"), AIMessage(content="ok")]
    )
    tool = FakeTool(name="read_note", responses=["y" * (MAX_TOOL_RESULT_CHARS * 3)])
    graph = build_graph(settings, router, [tool])

    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("clip-1"))

    answer = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert len(answer.content) < MAX_TOOL_RESULT_CHARS * 1.1
    assert "more characters were cut" in answer.content
