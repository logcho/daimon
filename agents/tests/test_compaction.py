"""Compaction: threshold gating, flash summarization, last-K preservation,
and the state write-back — unit-level, then through the agent node."""

from __future__ import annotations

from dataclasses import replace

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage

from daimon_agent.compaction import (
    KEEP_LAST,
    compact,
    compact_if_needed,
    context_tokens,
    estimate_size,
    estimate_tokens,
    projected_tokens,
    should_compact,
)
from daimon_agent.graph import build_graph, run_config
from daimon_agent.usage import CallUsage, UsageAccumulator, set_active_usage

from fakes import FakeRouter


def _chat(n: int) -> list:
    return [msg for i in range(n) for msg in (HumanMessage(content=f"q{i}"), AIMessage(content=f"a{i}"))]


def test_estimate_and_threshold() -> None:
    messages = _chat(5)
    assert estimate_size(messages) > 0
    tokens = estimate_tokens(messages)
    assert should_compact(messages, tokens - 1) is True
    assert should_compact(messages, tokens + 1) is False


def test_context_tokens_prefers_reported_usage() -> None:
    """The character estimate is only the cold start. Once a model call has
    reported real input tokens, that number wins — it accounts for the system
    prompt and tool schemas the message list doesn't show."""
    messages = _chat(2)
    assert context_tokens(messages) == estimate_tokens(messages)

    acc = UsageAccumulator()
    acc.add(CallUsage(model="m", input_tokens=9999, output_tokens=1), is_context=True)
    set_active_usage(acc)
    try:
        assert context_tokens(messages) == 9999
    finally:
        set_active_usage(None)


async def test_compact_summarizes_with_flash_and_keeps_recent(settings) -> None:
    router = FakeRouter(flash_script=[AIMessage(content="summary of the old tail")])
    messages = _chat(10)

    result = await compact(router, messages)

    assert result is not None
    assert len(router._flash.calls) == 1
    assert router._pro.calls == []  # cheap work never burns the pro role
    out = result.messages
    summary = [m for m in out if isinstance(m, SystemMessage)]
    assert len(summary) == 1
    assert "summary of the old tail" in summary[0].content
    # The last K messages survive verbatim, in order.
    assert out[-1].content == "a9"
    assert out[-2].content == "q9"
    # KEEP_LAST holds whole q/a pairs, so half of the kept window is human.
    assert sum(1 for m in out if isinstance(m, HumanMessage)) == KEEP_LAST // 2
    assert result.dropped == len(messages) - KEEP_LAST


async def test_compact_returns_a_wholesale_state_replacement(settings) -> None:
    """The update swaps the entire history rather than appending a summary —
    otherwise the checkpointer keeps the full transcript and the next hop
    re-summarizes the same prefix all over again."""
    router = FakeRouter(flash_script=[AIMessage(content="summary")])
    result = await compact(router, _chat(10))

    update = result.as_update()
    assert isinstance(update[0], RemoveMessage)
    assert update[1:] == result.replacement


async def test_compact_if_needed_is_a_noop_under_threshold(settings) -> None:
    router = FakeRouter()
    messages = _chat(2)  # small
    assert await compact_if_needed(settings, router, messages) is None
    assert router._flash.calls == []


async def test_compact_returns_none_on_an_empty_summary(settings) -> None:
    """Better a long context than a history dropped for nothing."""
    router = FakeRouter(flash_script=[AIMessage(content="   ")])
    assert await compact(router, _chat(10)) is None


async def test_agent_node_compacts_before_the_model_call(settings) -> None:
    small = replace(settings, compaction_tokens=1)
    router = FakeRouter(
        pro_script=[AIMessage(content="answer")],
        flash_script=[AIMessage(content="compressed history")],
    )
    graph = build_graph(small, router, [])

    turns = KEEP_LAST  # 2*KEEP_LAST messages — comfortably wider than the window
    final = await graph.ainvoke(
        {"messages": [HumanMessage(content="q")] + _chat(turns)}, run_config("compact")
    )

    assert len(router._flash.calls) == 1
    prompt = router._pro.calls[0]
    texts = [str(getattr(m, "content", "")) for m in prompt]
    assert any("compressed history" in t for t in texts)  # summary present
    assert any(f"a{turns - 1}" in t for t in texts)  # recent messages still there
    assert texts[0].startswith("You are Daimon")  # rules prefix still first

    # And the compaction landed in state: the pre-compaction messages are gone,
    # so a second hop starts from the summary instead of re-summarizing.
    kept = [str(getattr(m, "content", "")) for m in final["messages"]]
    assert any("compressed history" in t for t in kept)
    assert not any(t == "q0" for t in kept)


async def test_second_hop_does_not_resummarize(settings) -> None:
    """The regression this write-back exists for: one long turn used to call
    the summarizer once per agent-node visit, because the compacted list was
    never persisted and the next hop re-read the full history.

    Sized so the original conversation is over the threshold and the compacted
    one is comfortably under it — which is the whole premise of compacting.
    """
    from fakes import FakeTool, tool_call

    # 20 exchanges of ~500 chars ≈ 5700 tokens; the compacted result (summary
    # plus the last 6 messages) is ~900.
    long_chat = [
        msg
        for i in range(20)
        for msg in (
            HumanMessage(content=f"q{i} " + "x" * 500),
            AIMessage(content=f"a{i} " + "y" * 500),
        )
    ]
    small = replace(settings, compaction_tokens=3000)
    router = FakeRouter(
        pro_script=[
            tool_call("fake", {"arg": 1}, id="c1"),
            AIMessage(content="answer"),
        ],
        flash_script=[AIMessage(content="compressed history")],
    )
    graph = build_graph(small, router, [FakeTool(name="fake", responses=["ok"])])

    await graph.ainvoke({"messages": long_chat}, run_config("compact2"))

    # Two agent-node visits, one summarization.
    assert len(router._pro.calls) == 2
    assert len(router._flash.calls) == 1


# --- compacting before the over-budget request, not after ---------------------

def test_projected_size_counts_what_was_appended_since_the_last_call() -> None:
    """`context_tokens` measures the prompt as it stood one hop ago. Triggering
    on it alone means the first over-threshold request is always sent — the
    trigger only fires once it comes back."""
    sent = _chat(2)                       # what the last call carried
    appended = [AIMessage(content="x" * 7000)]   # a big tool hop since then

    acc = UsageAccumulator()
    acc.note_context_messages(len(sent))
    acc.add(CallUsage(model="m", input_tokens=1000, output_tokens=1), is_context=True)
    set_active_usage(acc)
    try:
        assert context_tokens(sent + appended) == 1000       # blind to the append
        assert projected_tokens(sent + appended) > 2500      # sees it
    finally:
        set_active_usage(None)


async def test_the_newest_instruction_survives_a_long_tool_loop(settings) -> None:
    """On a long turn the user's actual words fall out of the window early, and
    a summary of them is a poor stand-in for what the work is measured against."""
    from langchain_core.messages import ToolMessage

    router = FakeRouter(flash_script=[AIMessage(content="handover")])
    instruction = HumanMessage(content="rename the parser module")
    loop: list = []
    for i in range(KEEP_LAST + 4):
        loop.append(
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {}, "id": f"c{i}", "type": "tool_call"}],
            )
        )
        loop.append(ToolMessage(content=f"body {i}", tool_call_id=f"c{i}", name="read_file"))

    result = await compact(router, [instruction, *loop])

    assert result is not None
    assert any(
        isinstance(m, HumanMessage) and m.content == "rename the parser module"
        for m in result.replacement
    )


async def test_the_summary_asks_for_a_handover_not_a_paragraph(settings) -> None:
    router = FakeRouter(flash_script=[AIMessage(content="the handover")])
    await compact(router, _chat(KEEP_LAST + 4))

    asked = str(router._flash.calls[0][0].content)
    for section in ("## Goal", "## Decisions made", "## Files and state touched",
                    "## Open threads", "## Immediate next step"):
        assert section in asked
