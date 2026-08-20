"""Long-running turns.

The graph's recursion limit is a guard against infinite loops, not a budget for
how much work a task may take — a real project runs through it several times
over. Hitting it used to end the turn with an error the user had to answer by
re-prompting. Now the turn continues from the checkpoint, and `max_steps_per_turn`
is the ceiling that actually bounds an unattended run.

Real looping is still caught, by `guardrails.py` and by the budget below.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from daimon_agent.graph import (
    build_graph,
    close_checkpointer,
    make_sqlite_checkpointer,
    run_config,
)
from daimon_agent.run import resume_turn, run_turn

from fakes import FakeRouter, FakeTool, tool_call


@pytest.fixture
async def harness(settings, tmp_path):
    """A model that always calls a tool, so the turn only ends when something
    stops it. `steps` sets how many tool calls before it finally answers."""

    async def build(*, limit: int, budget: int, steps: int = 500):
        router = FakeRouter()
        router._pro.script = [tool_call("fake", {"n": i}, id=f"c{i}") for i in range(steps)]
        router._pro.script.append(AIMessage(content="all done"))
        tool = FakeTool(name="fake", responses=["ok"] * (steps + 10))
        cfg = replace(settings, recursion_limit=limit, max_steps_per_turn=budget)
        checkpointer = await make_sqlite_checkpointer(tmp_path / f"cp{limit}-{budget}.db")
        graph = build_graph(cfg, router, [tool], checkpointer=checkpointer)
        return cfg, graph, router, tool, checkpointer

    made: list = []

    async def factory(**kwargs):
        result = await build(**kwargs)
        made.append(result[-1])
        return result[:-1]

    yield factory
    for cp in made:
        await close_checkpointer(cp)


async def test_a_turn_continues_past_the_recursion_cap(harness) -> None:
    """The reported problem: a long task stopped every 40 steps and had to be
    re-prompted by hand."""
    cfg, graph, router, tool = await harness(limit=6, budget=1000, steps=20)
    events: list[dict] = []

    result = await run_turn("build it", "cont1", cfg, events.append, graph=graph)

    assert result == "all done"
    assert events[-1]["type"] == "done"
    assert not any(e["type"] == "error" for e in events), "the cap must not surface as an error"
    # It genuinely ran past one cap's worth of steps.
    assert len(tool.calls) == 20
    continuations = [e for e in events if e["type"] == "continuation"]
    assert continuations, "continuing should be visible, not silent"
    assert continuations[0]["steps"] > 0


async def test_continuation_events_report_progress(harness) -> None:
    cfg, graph, router, tool = await harness(limit=4, budget=1000, steps=30)
    events: list[dict] = []
    await run_turn("go", "cont2", cfg, events.append, graph=graph)

    steps = [e["steps"] for e in events if e["type"] == "continuation"]
    assert steps == sorted(steps), "step counts should climb"
    assert all(e["max_steps"] == 1000 for e in events if e["type"] == "continuation")


async def test_the_budget_stops_the_turn_and_asks(harness) -> None:
    """The ceiling on an unattended run: it asks rather than either erroring
    out or running forever."""
    cfg, graph, router, tool = await harness(limit=4, budget=8, steps=500)
    events: list[dict] = []

    result = await run_turn(
        "endless", "cont3", cfg, events.append, graph=graph, capabilities=["ask"]
    )

    assert result is None
    ask = events[-1]
    assert ask["type"] == "ask"
    assert ask["kind"] == "continue"
    assert [o["label"] for o in ask["options"]] == ["Continue", "Stop here"]
    assert not any(e["type"] == "done" for e in events)


async def test_answering_continue_resumes_the_same_turn(harness) -> None:
    """Each granted continuation buys another budget's worth of work — that's
    what the option says — so a long task may ask more than once. What matters
    is that every resume picks up where the last left off instead of starting
    over, and that the work eventually completes."""
    cfg, graph, router, tool = await harness(limit=4, budget=8, steps=12)
    events: list[dict] = []
    await run_turn("go", "cont4", cfg, events.append, graph=graph, capabilities=["ask"])
    assert events[-1]["type"] == "ask"

    result = None
    progress = [len(tool.calls)]
    for _ in range(10):
        if events[-1]["type"] != "ask":
            break
        result = await resume_turn("Continue", "cont4", cfg, events.append, graph=graph)
        progress.append(len(tool.calls))

    assert result == "all done"
    assert events[-1]["type"] == "done"
    assert len(tool.calls) == 12
    # Strictly increasing: no segment re-ran the previous one's work.
    assert progress == sorted(set(progress)), progress


async def test_answering_stop_reports_what_is_done(harness) -> None:
    """Declining wraps up with the work so far — it must not error, and must
    not keep running."""
    cfg, graph, router, tool = await harness(limit=4, budget=8, steps=500)
    first: list[dict] = []
    await run_turn("go", "cont5", cfg, first.append, graph=graph, capabilities=["ask"])
    calls_before = len(tool.calls)

    second: list[dict] = []
    result = await resume_turn("Stop here", "cont5", cfg, second.append, graph=graph)

    assert second[-1]["type"] == "done"
    assert result is not None
    assert len(tool.calls) == calls_before, "stopping should not run more tools"


async def test_without_the_ask_capability_it_stops_at_the_budget(harness) -> None:
    """A one-shot CLI or the chat app has nobody to answer, so parking on a
    question would read as a hang. It stops and reports instead."""
    cfg, graph, router, tool = await harness(limit=4, budget=8, steps=500)
    events: list[dict] = []

    result = await run_turn("endless", "cont6", cfg, events.append, graph=graph)

    assert not any(e["type"] == "ask" for e in events)
    assert events[-1]["type"] == "done"
    assert result is not None


async def test_a_normal_short_turn_is_untouched(harness) -> None:
    """No cap hit, no continuation noise."""
    cfg, graph, router, tool = await harness(limit=50, budget=1000, steps=1)
    events: list[dict] = []
    result = await run_turn("quick", "cont7", cfg, events.append, graph=graph)
    assert result == "all done"
    assert not any(e["type"] == "continuation" for e in events)


async def test_settings_drive_the_recursion_limit(settings) -> None:
    """rollout.py and the tests still use the module default; a turn uses the
    configured value."""
    from daimon_agent.graph import RECURSION_LIMIT

    assert run_config("s")["recursion_limit"] == RECURSION_LIMIT
    assert run_config("s", 7)["recursion_limit"] == 7


async def test_an_interrupt_still_resumes_as_an_interrupt(settings, tmp_path) -> None:
    """A graph interrupt and a continuation ask both end a turn resumably, but
    they resume differently — the state discriminates, so the client doesn't
    have to send anything extra."""
    router = FakeRouter(
        pro_script=[
            tool_call("ask_user", {"question": "Which?", "options": "A|first"}, id="c-ask"),
            AIMessage(content="picked A"),
        ]
    )
    checkpointer = await make_sqlite_checkpointer(tmp_path / "mix.db")
    graph = build_graph(settings, router, [], checkpointer=checkpointer)
    try:
        first: list[dict] = []
        await run_turn("pick", "mix", settings, first.append, graph=graph, capabilities=["ask"])
        assert first[-1]["kind"] == "question"

        second: list[dict] = []
        result = await resume_turn("A", "mix", settings, second.append, graph=graph)
    finally:
        await close_checkpointer(checkpointer)

    assert result == "picked A"
    # The answer reached the tool, which only happens on the Command(resume=)
    # path — a continuation resume would have discarded it.
    from langchain_core.messages import ToolMessage

    answered = [
        m for m in router._pro.calls[-1] if isinstance(m, ToolMessage) and m.name == "ask_user"
    ]
    assert answered and "A" in answered[0].content
