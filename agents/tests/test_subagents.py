"""The research fan-out: the main agent's research call is intercepted by the
tools node, stashed, and drained by the subagents node — one flash-model
subgraph run per query with a research-only tool set. Results rejoin as
ToolMessages; the step event for research stays running across the fan-out
and closes with the same id/label/tool (the run.ts invariant)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from daimon_agent.graph import build_graph, extract_result, run_config

from fakes import FakeRouter, tool_call


async def test_research_fans_out_to_flash_subagents(settings) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "What is the GDP of France?\nPopulation of Paris?"}, id="call-r1"),
            AIMessage(content="synthesized answer"),
        ],
        flash_script=[
            AIMessage(content="France GDP: $3.1 trillion."),
            AIMessage(content="Paris population: ~2.1 million."),
        ],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke(
        {"messages": [HumanMessage(content="research these")]}, run_config("fanout")
    )

    # Each subagent ran once, on the flash role; the main loop stayed on pro.
    assert len(router._flash.calls) == 2
    assert len(router._pro.calls) == 2

    # Both findings rejoined the main conversation as ToolMessages.
    messages = state["messages"]
    research_results = [m for m in messages if isinstance(m, ToolMessage) and m.name == "research"]
    assert len(research_results) == 2
    assert "GDP" in research_results[0].content
    assert "population" in research_results[1].content.lower()
    assert extract_result(messages) == "synthesized answer"


async def test_research_step_spans_the_fan_out(settings) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "Only one question"}, id="call-r1"),
            AIMessage(content="done"),
        ],
        flash_script=[AIMessage(content="the one answer")],
    )
    graph = build_graph(settings, router, [])
    events: list[dict] = []

    from daimon_agent.emitter import set_active_emit

    set_active_emit(events.append)
    try:
        await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("steps"))
    finally:
        set_active_emit(None)

    # Spawn event: running, label="research: query...", no tool (so TUI shows the query)
    running = [e for e in events if e.get("status") == "running"
               and isinstance(e.get("label"), str) and e["label"].startswith("research:")]
    # Done event: done, label="research", tool="research"
    done = [e for e in events if e.get("status") == "done" and e.get("label") == "research"]
    assert len(running) == len(done) == 1
    # Sub-id is "{call_id}-0" — the spawn and done events share the same id.
    assert running[0]["id"] == done[0]["id"]
    assert running[0]["id"] == "call-r1-0"
    assert "tool" not in running[0]  # spawn omits tool so TUI renders the query text
    assert done[0]["tool"] == "research"


async def test_research_with_no_queries_is_a_tool_message(settings) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "   \n  "}, id="call-empty"),
            AIMessage(content="recovered"),
        ],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke(
        {"messages": [HumanMessage(content="go")]}, run_config("empty")
    )
    assert router._flash.calls == []  # nothing to fan out
    warning = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert "no queries" in warning.content


async def test_research_counts_against_the_shared_budget(settings) -> None:
    """Budget exhausted -> the research call is blocked by the guardrail, the
    fan-out never starts, and the warning feeds back to the model."""
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "anything"}, id="call-blocked"),
            AIMessage(content="stopped"),
        ],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="go")],
            "research_used": 10,  # the era-1 hard stop, pre-seeded
        },
        run_config("budget"),
    )
    assert router._flash.calls == []
    warning = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert "Hard stop" in warning.content


async def test_research_is_absent_from_the_subagent_tool_set(settings) -> None:
    """A subagent asked to research must not recurse into another fan-out —
    the tool simply isn't in its registry (the subgraph answers directly
    instead of calling anything)."""
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "q1"}, id="call-r1"),
            AIMessage(content="done"),
        ],
        flash_script=[
            tool_call("research", {"queries": "nested"}, id="call-nest"),
            AIMessage(content="no nested fan-out happened"),
        ],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke(
        {"messages": [HumanMessage(content="go")]}, run_config("nested")
    )
    # The nested call hit "tool not available" inside the subgraph; the
    # subgraph recovered and answered — one extra flash call, no recursion.
    assert len(router._flash.calls) == 2
    research_results = [m for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "research"]
    assert len(research_results) == 1  # only the top-level fan-out rejoined
    assert research_results[0].content == "no nested fan-out happened"
