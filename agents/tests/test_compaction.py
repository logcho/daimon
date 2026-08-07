"""Compaction: threshold gating, flash summarization, last-K preservation —
unit-level, then through the agent node."""

from __future__ import annotations

from dataclasses import replace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from daimon_agent.compaction import compact, compact_if_needed, estimate_size, should_compact
from daimon_agent.graph import build_graph, run_config

from fakes import FakeRouter


def _chat(n: int) -> list:
    return [msg for i in range(n) for msg in (HumanMessage(content=f"q{i}"), AIMessage(content=f"a{i}"))]


def test_estimate_and_threshold() -> None:
    messages = _chat(5)
    size = estimate_size(messages)
    assert size > 0
    assert should_compact(messages, size - 1) is True
    assert should_compact(messages, size + 1) is False


async def test_compact_summarizes_with_flash_and_keeps_recent(settings) -> None:
    router = FakeRouter(flash_script=[AIMessage(content="summary of the old tail")])
    messages = _chat(10)

    out = await compact(router, messages)

    assert len(router._flash.calls) == 1
    assert router._pro.calls == []  # cheap work never burns the pro role
    summary = [m for m in out if isinstance(m, SystemMessage)]
    assert len(summary) == 1
    assert "summary of the old tail" in summary[0].content
    # The last K messages survive verbatim, in order.
    assert out[-1].content == "a9"
    assert out[-2].content == "q9"
    assert sum(1 for m in out if isinstance(m, HumanMessage)) == 3  # KEEP_LAST=6 holds 3 pairs


async def test_compact_if_needed_is_a_noop_under_threshold(settings) -> None:
    router = FakeRouter()
    messages = _chat(2)  # small
    out = await compact_if_needed(settings, router, messages)
    assert out == messages
    assert router._flash.calls == []


async def test_agent_node_compacts_before_the_model_call(settings) -> None:
    small = replace(settings, compaction_chars=10)
    router = FakeRouter(
        pro_script=[AIMessage(content="answer")],
        flash_script=[AIMessage(content="compressed history")],
    )
    graph = build_graph(small, router, [])

    await graph.ainvoke(
        {"messages": [HumanMessage(content="q")] + _chat(6)}, run_config("compact")
    )

    assert len(router._flash.calls) == 1
    prompt = router._pro.calls[0]
    texts = [str(getattr(m, "content", "")) for m in prompt]
    assert any("compressed history" in t for t in texts)  # summary present
    assert any("a5" in t for t in texts)  # recent messages still there
    assert texts[0].startswith("You are Daimon")  # rules prefix still first
