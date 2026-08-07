"""Headless rollout — run a task through the real graph with a given skill
tail and return the final result text. The skill (or its absence) is the only
thing that varies between runs, so score deltas are attributable to it.

Rollouts use the full tool set: a skill that teaches read_file/write_file
must actually be able to use them. The emitter is a silent no-op when unset
(run_turn semantics), so nothing escapes to a UI during optimization.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from daimon_agent.graph import RECURSION_LIMIT, build_graph, extract_result

_SESSION = "optimizer-rollout"


async def rollout(
    settings: Any,
    router: Any,
    tools: list[Any],
    task: dict,
    skills_block: str,
) -> str:
    """One task under one skill tail, headless. `tools` is the full tool
    registry (build_tools output); no checkpointer is needed — ainvoke's
    return state carries the final messages."""
    graph = build_graph(settings, router, tools)
    config = {"recursion_limit": RECURSION_LIMIT}
    final = await graph.ainvoke(
        {
            "messages": [HumanMessage(content=task["instruction"])],
            "instruction": task["instruction"],
            "session_id": _SESSION,
            "skills_block": skills_block,
        },
        config,
    )
    return extract_result(final["messages"])
