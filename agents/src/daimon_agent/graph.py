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
from uuid import uuid4

import aiosqlite
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .compaction import compact_if_needed
from .emitter import _active, emit, set_active_emit
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
from collections.abc import Callable

# Era-1 value, kept.
RECURSION_LIMIT = 40
# Subagents get their own, tighter budget — a research fan-out is bounded
# work, not a second full agent run.
SUBAGENT_RECURSION_LIMIT = 15
MAX_RESEARCH_FAN_OUT = 3
# The research-only tool set delegated to each subagent (no nested research).
RESEARCH_SUBAGENT_TOOLS = frozenset({"web_search", "open_url", "read_page", "extract_text", "web_fetch"})


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
    # Two daimon-agent processes (app + CLI) may share this file — wait up
    # to 10s for the other's write lock instead of erroring (WAL is already
    # set lazily by AsyncSqliteSaver.setup()).
    await conn.execute("PRAGMA busy_timeout = 10000")
    return AsyncSqliteSaver(conn)


async def close_checkpointer(checkpointer: Any) -> None:
    """Close the sqlite connection behind the saver. The aiosqlite worker
    thread is non-daemon — without an explicit close the process hangs at
    interpreter exit (the saver's own docs: 'you may see the graph hang').
    Safe to call on a None or connection-less checkpointer."""
    conn = getattr(checkpointer, "conn", None)
    if conn is not None:
        await conn.close()


def _repair_orphaned_tool_calls(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Inject synthetic error ToolMessages for any AIMessage tool_calls that
    aren't followed by the corresponding ToolMessages. This heals state that
    was interrupted mid-turn (killed process, crashed checkpointer, etc.) so
    the model API doesn't reject the conversation with 'insufficient tool
    messages following tool_calls message'."""
    repaired: list[AnyMessage] = []
    for i, msg in enumerate(messages):
        repaired.append(msg)
        if not isinstance(msg, AIMessage) or not msg.tool_calls:
            continue
        # Collect tool_call_ids that have a corresponding ToolMessage after
        required = {call["id"] for call in msg.tool_calls}
        for later in messages[i + 1:]:
            if isinstance(later, ToolMessage) and later.tool_call_id in required:
                required.discard(later.tool_call_id)
        # Inject a synthetic error for each orphaned call
        for missing_id in sorted(required):
            repaired.append(
                ToolMessage(
                    content="(This tool call was interrupted — the agent process was killed or restarted before it could complete.)",
                    tool_call_id=missing_id,
                    name="unknown",
                )
            )
    return repaired


def build_graph(
    settings: Any,
    router: ModelRouter,
    tools: list[BaseTool],
    *,
    memory: Any = None,
    checkpointer: Any = None,
    role: str = "pro",
    prompt_builder: Callable[..., str] | None = None,
) -> CompiledStateGraph:
    """Build the compiled graph. `checkpointer` defaults to an
    AsyncSqliteSaver on settings.checkpoints_db; the caller owns its lifetime
    (one per process). `role` selects the model: "pro" (main agent, hybrid
    routing's primary role) or "flash" (research subagents — cheap, bounded
    work where a missed call is cheap to retry). `prompt_builder` overrides
    the default system prompt (used by the coding agent for its kernel-first
    doctrine)."""

    tool_by_name = {t.name: t for t in tools}
    subgraph = None
    if role == "pro":
        research_tools = [t for t in tools if t.name in RESEARCH_SUBAGENT_TOOLS]
        subgraph = build_graph(settings, router, research_tools, role="flash")

    async def agent_node(state: AgentState) -> dict:
        # Lazy: the model is constructed on the first call, not at graph-build
        # time, so a keyless process can still boot and serve /health. The
        # router caches instances; bind_tools per node call is cheap.
        model = (router.pro() if role == "pro" else router.flash()).bind_tools(tools)
        _build_prompt = prompt_builder or build_system_prompt
        system_prompt = _build_prompt(settings, skills_block=state.get("skills_block", ""))
        # Compaction (token-threshold summarization via flash) applies only to
        # the main agent — subgraph turns are bounded by their own recursion.
        messages = (
            await compact_if_needed(settings, router, state["messages"])
            if role == "pro"
            else list(state["messages"])
        )
        # Heal interrupted state — if the last turn was killed mid-execution,
        # orphaned tool_calls would cause the model API to reject the request.
        messages = _repair_orphaned_tool_calls(messages)
        messages = [SystemMessage(content=system_prompt), *messages]
        response = await model.ainvoke(messages)
        # When the model sends text alongside tool calls (e.g. "Let me first
        # read the file to understand it"), surface it as a reasoning step so
        # the user sees the agent's plan before it starts executing.
        if response.content and isinstance(response.content, str):
            text = response.content.strip()
            if text and response.tool_calls:
                emit(step_event(str(uuid4()), f"Reasoning: {text[:200]}", "done", None))
        for call in response.tool_calls:
            name = call.get("name", "tool")
            emit(step_event(call["id"], name, "running", name))
        return {"messages": [response]}

    async def tools_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        results: list[ToolMessage] = []
        pending: list[tuple[str, str, str]] = list(state.get("research_pending", []))
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

            # --- research fan-out: delegated to subagents, not executed here -
            if name == "research":
                queries = [
                    q.strip()
                    for q in str(args.get("queries", "")).splitlines()
                    if q.strip()
                ][:MAX_RESEARCH_FAN_OUT]
                if not queries:
                    results.append(
                        ToolMessage(
                            content="research: no queries found — put one research question per line.",
                            tool_call_id=call_id,
                            name=name,
                        )
                    )
                    emit(step_event(call_id, name, "done", name))
                    continue
                for i, query in enumerate(queries):
                    sub_id = f"{call_id}-{i}"
                    pending.append((call_id, sub_id, name, query))
                # One budget slot for the fan-out, like any other research call.
                research_used += 1
                call_log.append((name, json.dumps(args, sort_keys=True, default=str)))
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
            "research_pending": pending,
        }

    async def subagent_node(state: AgentState) -> dict:
        """Drain the research fan-out: one flash-model subgraph run per query.
        Each run has its own recursion budget and a research-only tool set;
        results rejoin the main conversation as ToolMessages.

        Spawn and completion events use *sub_id* (unique per query) so the TUI
        can independently track each sub-agent's lifecycle.  The emit wrapper
        attaches ``parent_step_id`` and ``subagent_query`` to every event the
        subgraph produces — tool steps, Thinking, etc. — so the TUI can render
        them indented beneath the spawn line."""
        results: list[ToolMessage] = []
        for call_id, sub_id, name, query in state.get("research_pending", []):
            # Spawn event — TUI shows "→ research: query..." while running.
            # No tool field so the TUI renders `name = label` with the query text.
            emit(step_event(sub_id, f"research: {query[:60]}", "running"))

            # Wrap emit so every event from inside the subgraph carries parent
            # context — the TUI uses this to indent sub-agent tool steps.
            original_emit = _active.get()
            def sub_emit(event: dict) -> None:
                if event.get("type") == "step":
                    event["parent_step_id"] = sub_id
                    event["subagent_query"] = query[:80]
                if original_emit is not None:
                    original_emit(event)

            set_active_emit(sub_emit)
            status = "done"
            try:
                final = await subgraph.ainvoke(
                    {
                        "messages": [HumanMessage(content=query)],
                        "instruction": query,
                        "session_id": f"subagent-{sub_id}",
                    },
                    {"recursion_limit": SUBAGENT_RECURSION_LIMIT},
                )
                answer = extract_result(final["messages"])
                results.append(ToolMessage(content=answer, tool_call_id=call_id, name=name))
            except Exception as exc:
                results.append(
                    ToolMessage(
                        content=f"research sub-agent failed: {exc}",
                        tool_call_id=call_id,
                        name=name,
                        status="error",
                    )
                )
                status = "error"
            finally:
                set_active_emit(original_emit)

            # Done/error event at the top level (after restoring emit) so the
            # TUI replaces the spawn line — sub_id matches the running event.
            emit(step_event(sub_id, name, status, name))
        return {"messages": results, "research_pending": []}

    def route_after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return END

    def route_after_tools(state: AgentState) -> str:
        if state.get("research_pending"):
            return "subagents"
        return "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    if subgraph is not None:
        graph.add_node("subagents", subagent_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", END: END})
    if subgraph is not None:
        graph.add_conditional_edges(
            "tools", route_after_tools, {"subagents": "subagents", "agent": "agent"}
        )
        graph.add_edge("subagents", "agent")
    else:
        graph.add_edge("tools", "agent")
    compiled = graph.compile(checkpointer=checkpointer)
    compiled.tools = tools  # attached for the /tools HTTP endpoint
    return compiled


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
