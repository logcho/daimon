"""The agent graph — a custom StateGraph forked from create_react_agent's
structure (agent -> tools loop), with guardrails owned by the tools node.

Why not the prebuilt agent: the NDJSON contract needs precise per-tool-call
lifecycle events (running at model-call time, done/error after execution) and
guardrail pre-checks *before* execution — that requires owning the node
bodies, which the prebuilt doesn't expose. The era-1 lesson stands: the
anti-loop machinery lives in the graph (guardrails.py, applied here), and the
checkpointer owns conversation history, so there is no module-global
conversation array and no rollback hack.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .emitter import emit
from .events import step_event
from .guardrails import (
    check_near_duplicate,
    check_page_unchanged,
    check_repeat,
    check_research_budget,
    RESEARCH_TOOLS,
)
from .model import ModelRouter
from .prompts import build_system_prompt
from .state import AgentState

# Era-1 value, kept.
RECURSION_LIMIT = 40


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


async def make_sqlite_checkpointer(path: Path) -> AsyncSqliteSaver:
    """One saver per process — it holds a shared connection; never construct
    it per request. setup() runs lazily on first checkpoint write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    return AsyncSqliteSaver(conn)


async def close_checkpointer(checkpointer: Any) -> None:
    """Close the sqlite connection behind the saver. The aiosqlite worker
    thread is non-daemon — without an explicit close the process hangs at
    interpreter exit (the saver's own docs: 'you may see the graph hang').
    Safe to call on a None or connection-less checkpointer."""
    conn = getattr(checkpointer, "conn", None)
    if conn is not None:
        await conn.close()


def build_graph(
    settings: Any,
    router: ModelRouter,
    tools: list[BaseTool],
    *,
    memory: Any = None,
    checkpointer: Any = None,
) -> CompiledStateGraph:
    """Build the compiled graph. `checkpointer` defaults to an
    AsyncSqliteSaver on settings.checkpoints_db; the caller owns its lifetime
    (one per process)."""

    tool_by_name = {t.name: t for t in tools}

    async def agent_node(state: AgentState) -> dict:
        # Lazy: the model is constructed on the first call, not at graph-build
        # time, so a keyless process can still boot and serve /health. The
        # router caches instances; bind_tools per node call is cheap.
        model = router.pro().bind_tools(tools)
        system_prompt = build_system_prompt(settings, skills_block=state.get("skills_block", ""))
        messages: list[AnyMessage] = [SystemMessage(content=system_prompt), *state["messages"]]
        response = await model.ainvoke(messages)
        for call in response.tool_calls:
            name = call.get("name", "tool")
            emit(step_event(call["id"], name, "running", name))
        return {"messages": [response]}

    async def tools_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        results: list[ToolMessage] = []
        research_used = state.get("research_used", 0)
        call_log = list(state.get("call_log", []))
        last_sig = state.get("last_read_signature")

        for call in last.tool_calls:
            name = call.get("name", "")
            args = call.get("args") or {}
            tool = tool_by_name.get(name)
            call_id = call.get("id", f"call_{len(results)}")

            # --- guardrail pre-check (never executes on a warning) ----------
            if name in RESEARCH_TOOLS:
                warning = (
                    check_research_budget(name, research_used)
                    or check_repeat(name, args, call_log)
                )
                if name == "web_search":
                    warning = warning or check_near_duplicate(str(args.get("query", "")), call_log)
                if warning:
                    results.append(ToolMessage(content=warning, tool_call_id=call_id, name=name))
                    emit(step_event(call_id, name, "done", name))
                    continue

            if tool is None:
                message = f'Tool "{name}" is not available in this session.'
                results.append(
                    ToolMessage(content=message, tool_call_id=call_id, name=name, status="error")
                )
                emit(step_event(call_id, name, "error", name))
                continue

            try:
                content = _tool_result_text(await tool.ainvoke(args))
            except Exception as exc:  # a tool failure is a message, not a crash
                message = f"{name} failed: {exc}"[:2000]
                results.append(
                    ToolMessage(content=message, tool_call_id=call_id, name=name, status="error")
                )
                emit(step_event(call_id, name, "error", name))
                continue

            # --- post-execution bookkeeping for research tools ---------------
            if name in RESEARCH_TOOLS:
                research_used += 1
                call_log.append((name, json.dumps(args, sort_keys=True, default=str)))
                if name == "read_page":
                    warning, last_sig = check_page_unchanged(last_sig, content)
                    if warning:
                        # The read executed but returned nothing new — surface
                        # the warning instead of the content, like era-1.
                        content = warning

            results.append(ToolMessage(content=content, tool_call_id=call_id, name=name))
            emit(step_event(call_id, name, "done", name))

        return {
            "messages": results,
            "research_used": research_used,
            "call_log": call_log,
            "last_read_signature": last_sig,
        }

    def route_after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)


def run_config(session_id: str) -> dict:
    """The config every turn runs with: checkpointer thread + recursion cap."""
    return {"configurable": {"thread_id": session_id}, "recursion_limit": RECURSION_LIMIT}


def extract_result(messages: list[AnyMessage]) -> str:
    """The last non-blank assistant text, else the legacy fallback. Like
    legacy's `.trim()`, whitespace-only results are treated as empty."""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content:
            text = _tool_result_text(message.content)
            if text.strip():
                return text.strip()
    return "Task complete."
