"""The sub-agent fan-out: `research` and `task` calls are intercepted by the
tools node, stashed, and drained by the subagents node — one flash-model
subgraph run per entry, all concurrently, each with its own tool set. Results
rejoin as ToolMessages; the step event stays running across the fan-out and
closes with the same id/label/tool (the run.ts invariant)."""

from __future__ import annotations

import time

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

    # Both findings rejoined the main conversation — as ONE ToolMessage, since
    # they answer a single `research` tool_call. Answering one call twice makes
    # the conversation unsendable (see test_sanitize.py).
    messages = state["messages"]
    research_results = [m for m in messages if isinstance(m, ToolMessage) and m.name == "research"]
    assert len(research_results) == 1
    body = research_results[0].content
    assert "GDP" in body
    assert "population" in body.lower()
    # Each finding is labelled with the question that produced it.
    assert "[What is the GDP of France?]" in body
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


async def test_fan_out_runs_concurrently(settings) -> None:
    """Three sub-agents that each block for 60ms must finish in roughly 60ms,
    not 180ms. This is the property the tool description promises and that the
    old sequential `for … await` loop quietly didn't deliver."""
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "q1\nq2\nq3"}, id="call-r1"),
            AIMessage(content="synthesized"),
        ],
    )
    router._flash.delay = 0.06
    graph = build_graph(settings, router, [])

    started = time.monotonic()
    await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("concurrent"))
    elapsed = time.monotonic() - started

    assert len(router._flash.calls) == 3
    # Generous bound: sequential would be >= 0.18s, concurrent ~0.06s.
    assert elapsed < 0.15, f"fan-out took {elapsed:.3f}s — it ran sequentially"


async def test_concurrent_subagent_events_carry_their_own_agent_id(settings) -> None:
    """Each sub-agent stamps its own id on its events. Without this, three
    agents' steps interleave into one unreadable stream — which is exactly
    what concurrency would otherwise cost the display."""
    router = FakeRouter(
        pro_script=[
            tool_call("research", {"queries": "q1\nq2"}, id="call-r1"),
            AIMessage(content="done"),
        ],
        # Each sub-agent reaches for a tool it doesn't have, which still emits a
        # step — enough to show whose events they are.
        flash_script=[
            tool_call("web_search", {"query": "q1"}, id="sub-call-1"),
            AIMessage(content="answer 1"),
            tool_call("web_search", {"query": "q2"}, id="sub-call-2"),
            AIMessage(content="answer 2"),
        ],
    )
    graph = build_graph(settings, router, [])
    events: list[dict] = []

    from daimon_agent.emitter import set_active_emit

    set_active_emit(events.append)
    try:
        await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("ids"))
    finally:
        set_active_emit(None)

    spawns = [e for e in events if e.get("agent_label") == "research"]
    assert {e["agent_id"] for e in spawns} == {"call-r1-0", "call-r1-1"}
    # Everything the subgraph itself emitted is attributed to its parent spawn.
    nested = [e for e in events if e.get("parent_step_id")]
    assert nested and all(
        e["parent_step_id"] in ("call-r1-0", "call-r1-1") for e in nested
    )


async def test_task_tool_spawns_one_agent_of_the_named_type(settings) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call(
                "task",
                {"agent_type": "explore", "prompt": "Find where emit is defined"},
                id="call-t1",
            ),
            AIMessage(content="it's in emitter.py"),
        ],
        flash_script=[AIMessage(content="emitter.py defines emit()")],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke(
        {"messages": [HumanMessage(content="go")]}, run_config("task")
    )

    assert len(router._flash.calls) == 1
    results = [m for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "task"]
    assert len(results) == 1
    assert results[0].content == "emitter.py defines emit()"
    assert extract_result(state["messages"]) == "it's in emitter.py"


async def test_task_with_an_unknown_agent_type_falls_back(settings) -> None:
    """A bad agent_type costs a slightly wrong tool set, not a failed turn."""
    router = FakeRouter(
        pro_script=[
            tool_call("task", {"agent_type": "wizard", "prompt": "do a thing"}, id="call-t1"),
            AIMessage(content="fine"),
        ],
        flash_script=[AIMessage(content="did the thing")],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("badtype"))
    results = [m for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "task"]
    assert results[0].content == "did the thing"


async def test_task_with_an_empty_prompt_is_a_tool_message(settings) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call("task", {"agent_type": "general", "prompt": "  "}, id="call-t1"),
            AIMessage(content="recovered"),
        ],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("noprompt"))
    assert router._flash.calls == []
    warning = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert "prompt is required" in warning.content


async def test_task_is_absent_from_the_subagent_tool_set(settings) -> None:
    """No nesting: a sub-agent can't spawn sub-agents, so a fan-out of
    fan-outs has no way to escape its budget."""
    router = FakeRouter(
        pro_script=[
            tool_call("task", {"agent_type": "general", "prompt": "outer"}, id="call-t1"),
            AIMessage(content="done"),
        ],
        flash_script=[
            tool_call("task", {"agent_type": "general", "prompt": "inner"}, id="call-nest"),
            AIMessage(content="no nested spawn happened"),
        ],
    )
    graph = build_graph(settings, router, [])
    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("nest2"))
    results = [m for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "task"]
    assert len(results) == 1
    assert results[0].content == "no nested spawn happened"


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
