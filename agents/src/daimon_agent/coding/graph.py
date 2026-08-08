"""Coding-agent graph — same StateGraph structure as the general agent
(agent → tools loop, compaction, NDJSON events), but with a kernel-first
system prompt and a tool set optimized for coding work (no browser, no
research fan-out, direct shell execution).
"""

from __future__ import annotations

from typing import Any

from langgraph.graph.state import CompiledStateGraph

from ..graph import build_graph, make_sqlite_checkpointer
from ..model import ModelRouter
from .prompts import build_coding_system_prompt
from .tools import build_coding_tools


async def build_coding_graph(
    settings: Any,
    router: ModelRouter,
    *,
    memory: Any = None,
    checkpointer: Any = None,
) -> CompiledStateGraph:
    """Build the compiled coding-agent graph. Same infrastructure as the general
    agent — one checkpointer, one memory store, one event contract — but with
    coding tools (kernel + shell + file ops + search) and kernel-first prompt."""
    tools = build_coding_tools(settings, memory=memory)
    return build_graph(
        settings,
        router,
        tools,
        memory=memory,
        checkpointer=checkpointer,
        prompt_builder=build_coding_system_prompt,
    )
