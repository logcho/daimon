"""Structural validity of the conversation sent to the model.

The API enforces exactly one response per tool_call, each answering a call that
came before it. Every test here is a way that invariant broke in practice — the
`research` fan-out answering one call N times, and compaction cutting through a
tool batch. The second is the dangerous one: compaction rewrites *stored*
history, so an invalid list isn't one bad request, it's a session that can
never make a valid request again.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from daimon_agent.compaction import _compact_keep
from daimon_agent.graph import build_graph, run_config, sanitize_messages

from fakes import FakeRouter, tool_call


def assert_valid(messages: list) -> None:
    """The pairing rule the model API applies."""
    open_calls: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            open_calls = {c["id"] for c in (message.tool_calls or [])}
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in open_calls, (
                f"tool message {message.tool_call_id!r} answers no preceding tool_call"
            )
            open_calls.discard(message.tool_call_id)


def _ai(*call_ids: str, content: str = "") -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[
            {"name": "t", "args": {}, "id": cid, "type": "tool_call"} for cid in call_ids
        ],
    )


def _tool(call_id: str, content: str = "r") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id, name="t")


# --- the three break modes ---------------------------------------------------

def test_unanswered_call_gets_a_synthetic_result() -> None:
    """The process died mid-turn and the results never landed."""
    out = sanitize_messages([_ai("c1", "c2"), _tool("c1")])
    assert_valid(out)
    assert [m.tool_call_id for m in out if isinstance(m, ToolMessage)] == ["c1", "c2"]
    assert "interrupted" in out[-1].content


def test_orphaned_response_is_dropped() -> None:
    """Compaction dropped the AIMessage but kept its results — the cut has to
    land somewhere."""
    out = sanitize_messages([_tool("ghost"), HumanMessage(content="hi")])
    assert_valid(out)
    assert not any(isinstance(m, ToolMessage) for m in out)


def test_duplicate_responses_keep_only_the_first() -> None:
    """What the research fan-out used to produce: one call, N answers."""
    out = sanitize_messages([_ai("c1"), _tool("c1", "first"), _tool("c1", "second")])
    assert_valid(out)
    tools = [m for m in out if isinstance(m, ToolMessage)]
    assert len(tools) == 1 and tools[0].content == "first"


def test_a_valid_conversation_is_left_alone() -> None:
    messages = [
        HumanMessage(content="q"),
        _ai("c1", "c2"),
        _tool("c1"),
        _tool("c2"),
        AIMessage(content="answer"),
    ]
    assert sanitize_messages(messages) == messages


def test_sanitizing_is_idempotent() -> None:
    once = sanitize_messages([_tool("ghost"), _ai("c1", "c2"), _tool("c1"), _tool("c1")])
    assert sanitize_messages(once) == once
    assert_valid(once)


def test_empty_and_toolless_conversations() -> None:
    assert sanitize_messages([]) == []
    plain = [HumanMessage(content="hi"), AIMessage(content="hello")]
    assert sanitize_messages(plain) == plain


# --- the reported failure ----------------------------------------------------

def test_compaction_cut_through_a_tool_batch_stays_valid() -> None:
    """The reported crash: a research turn, then compaction, then every
    subsequent call 400s with "must be a response to a preceding message with
    'tool_calls'"."""
    history = [
        HumanMessage(content="old"),
        _ai("call-r0"),
        _tool("call-r0", "A"),
        AIMessage(content="old answer"),
        HumanMessage(content="new"),
        _ai("c1", "c2"),
        _tool("c1"),
        _tool("c2"),
        AIMessage(content="done"),
    ]
    for keep_from in range(len(history)):
        # Whatever the cut, the result must be sendable.
        assert_valid(sanitize_messages(history[keep_from:]))
    assert_valid(sanitize_messages(_compact_keep(history)))


# --- the source fix ----------------------------------------------------------

async def test_fan_out_produces_one_tool_message_per_call(settings) -> None:
    """Three sub-agents, one `research` tool_call, one response. Answering a
    single call three times is what made the conversation unsendable."""
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "q1\nq2\nq3"}, id="call-r1"),
            AIMessage(content="synthesized"),
        ],
        flash_script=[
            AIMessage(content="finding one"),
            AIMessage(content="finding two"),
            AIMessage(content="finding three"),
        ],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke(
        {"messages": [HumanMessage(content="go")]}, run_config("merge")
    )

    assert_valid(state["messages"])
    tools = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(tools) == 1
    # Each finding is labelled with the question that produced it, or the model
    # has three answers and no way to tell them apart.
    assert "[q1]" in tools[0].content and "[q3]" in tools[0].content
    assert "finding one" in tools[0].content and "finding three" in tools[0].content


async def test_a_single_query_fan_out_is_not_relabelled(settings) -> None:
    """One query needs no disambiguation — don't decorate it."""
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "only one"}, id="call-r1"),
            AIMessage(content="done"),
        ],
        flash_script=[AIMessage(content="the finding")],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke(
        {"messages": [HumanMessage(content="go")]}, run_config("single")
    )
    tools = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert tools[0].content == "the finding"


async def test_research_then_compaction_then_another_turn(settings, tmp_path) -> None:
    """End to end, the reported sequence: fan-out, compaction, keep going."""
    from dataclasses import replace

    from daimon_agent.graph import close_checkpointer, make_sqlite_checkpointer
    from daimon_agent.run import run_turn

    small = replace(settings, compaction_tokens=200)
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "q1\nq2"}, id="call-r1"),
            AIMessage(content="x" * 4000),          # blows past the threshold
            AIMessage(content="second turn answer"),
        ],
        flash_script=[
            AIMessage(content="y" * 2000),
            AIMessage(content="z" * 2000),
            AIMessage(content="a summary"),
        ],
    )
    checkpointer = await make_sqlite_checkpointer(tmp_path / "cp.db")
    graph = build_graph(small, router, [], checkpointer=checkpointer)
    try:
        first = await run_turn("compare these", "e2e", small, [].append, graph=graph)
        second = await run_turn("and now this", "e2e", small, [].append, graph=graph)
        state = await graph.aget_state(run_config("e2e"))
    finally:
        await close_checkpointer(checkpointer)

    assert first is not None
    assert second == "second turn answer", "the turn after compaction failed"
    assert_valid(state.values["messages"])
