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

from fakes import FakeRouter, FakeTool, tool_call


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
    # Two daimon-agent processes (app + CLI) may share the checkpoints file —
    # the pragma value IS the contract: wait, don't error.
    checkpointer = await make_sqlite_checkpointer(settings.checkpoints_db)
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
