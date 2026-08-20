"""The agent graph — a custom StateGraph forked from create_react_agent's
structure (agent -> tools loop), with guardrails owned by the tools node.

Why not the prebuilt agent: the NDJSON contract needs precise per-tool-call
lifecycle events (running at model-call time, done/error after execution) and
guardrail pre-checks *before* execution — that requires owning the node
bodies, which the prebuilt doesn't expose. The era-1 lesson stands: the
anti-loop machinery lives in the graph (guardrails.py, applied here), and the
checkpointer owns conversation history, so there is no module-global
conversation array and no rollback hack.

Three things here are worth knowing before editing:

- **The agent node streams.** Text reaches the user as the model writes it,
  which is also why the model call is wrapped rather than a bare `ainvoke`.
- **Sub-agents run concurrently**, one asyncio task each. Event routing works
  because a task inherits a *copy* of the context, so each sub-agent's
  `set_active_emit` is invisible to its siblings.
- **`ask_user`/`present_plan` suspend the graph** via `interrupt()`, resolved
  at the *top* of the tools node before anything executes. Resuming re-runs
  the node from the start, so no tool may have run yet or it would run twice.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import interrupt

from .compaction import compact_if_needed
from .emitter import _active, emit, set_active_emit
from .events import (
    ask_event,
    assistant_delta_event,
    compaction_event,
    step_event,
    usage_event,
)
from .guardrails import (
    check_near_duplicate,
    check_page_unchanged,
    check_repeat,
    check_research_budget,
    REPEAT_EXEMPT,
    RESEARCH_TOOLS,
)
from .model import ModelRouter
from .prompts import build_system_prompt
from .providers import supports_prompt_cache_control
from .state import AgentState
from .tools.ask import ASK_TOOLS, parse_options
from .usage import extract_usage, note_context_messages, record
from collections.abc import Callable

# Era-1 value, kept.
RECURSION_LIMIT = 40
# Subagents get their own, tighter budget — a research fan-out is bounded
# work, not a second full agent run.
SUBAGENT_RECURSION_LIMIT = 15
MAX_RESEARCH_FAN_OUT = 3
# `task` spawns are a little more generous than `research`: they do real work
# rather than one search each, and they now run concurrently, so the cost of
# one more is latency-free.
MAX_SUBAGENT_FAN_OUT = 4

# The research-only tool set delegated to each subagent (no nested research).
RESEARCH_SUBAGENT_TOOLS = frozenset({"web_search", "open_url", "read_page", "extract_text", "web_fetch"})
# Read-only file tools — an `explore` subagent maps the codebase and reports
# back without the parent paying context for everything it read.
EXPLORE_SUBAGENT_TOOLS = frozenset({"read_file", "glob_files", "grep_files", "list_directory"})
# Tools no subagent ever gets: spawning (no nesting — a fan-out of fan-outs
# has no budget that holds) and asking (only the main agent talks to the user).
NON_DELEGABLE_TOOLS = frozenset({"research", "task"}) | ASK_TOOLS

#: agent_type → the tools it gets. None means "everything delegable".
SUBAGENT_TOOLSETS: dict[str, frozenset[str] | None] = {
    "research": RESEARCH_SUBAGENT_TOOLS,
    "explore": EXPLORE_SUBAGENT_TOOLS,
    "general": None,
}
DEFAULT_AGENT_TYPE = "research"

#: Tools that change something outside the conversation. In plan mode these
#: are gated behind an approved plan — enforced here rather than left to the
#: prompt, because a model that ignores an instruction to ask first has
#: already done the thing by the time you find out.
MUTATING_TOOLS = frozenset({
    "write_file", "edit_file", "delete_file", "move_file", "mkdir",
    "run_shell", "kernel_execute", "save_skill", "stage_terminal_command",
})

MALFORMED_CALL = (
    "{tool} was not run: its arguments did not parse ({error}). Every argument in "
    "this tool set is a flat string — no nested objects, no trailing commas, no "
    "unquoted values. Re-issue the call with valid JSON, or use a different tool."
)

#: Tools that may run at the same time as their neighbours. Strictly read-only:
#: they touch no shared mutable state, so two of them in flight together cannot
#: observe each other.
#:
#: What is deliberately absent matters more than what is here. RESEARCH_TOOLS
#: stay sequential because their budget and duplicate-detection bookkeeping is
#: order-dependent. `kernel_execute` and `run_shell` stay sequential because
#: they share a kernel and a filesystem. Every MUTATING_TOOLS entry stays
#: sequential by definition.
CONCURRENT_SAFE_TOOLS = frozenset({
    "read_file", "glob_files", "grep_files", "list_directory",
    "read_note", "list_notes",
    "read_skill", "list_skills",
    "recall",
})

PLAN_REQUIRED = (
    "Plan mode is on and you have not had a plan approved yet, so {tool} did not "
    "run and nothing was changed. Call present_plan with what you intend to do — "
    "the steps, the files you'll touch, anything you're assuming — and wait for "
    "the user's answer before trying again. Reading and searching are still fine."
)


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


#: Ceiling on any single tool result, in characters. Individual tools have
#: their own limits — 40k for a file read, 8k for a shell command, 12k for a
#: web fetch — but they are inconsistent, and several tools (a note, a skill,
#: a registry search) had none at all. Those are the ones that surprise you: a
#: 200KB note read into a 60k-token budget is a wasted turn. This is the
#: backstop under all of them, applied where every result passes through.
MAX_TOOL_RESULT_CHARS = 25_000


def clip_result(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Cut an oversized tool result, saying what was cut and what to do about it.

    The message matters as much as the cut. A result that simply stops invites
    the model to run the same call again and hope; naming the narrowing move
    points it at the tool arguments instead.
    """
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return (
        text[:limit]
        + f"\n\n… ({dropped:,} more characters were cut here. Narrow the request — a "
        f"line range, a subdirectory, a more specific query — rather than repeating "
        f"this call unchanged.)"
    )


#: Per-tool, the argument worth showing next to the tool name. First match
#: wins; a tool with no entry shows no detail rather than a dump of its args.
_DETAIL_KEYS = (
    "path", "file_path", "name", "query", "queries", "command", "url", "pattern", "prompt",
)


def _call_detail(args: dict, limit: int = 72) -> str | None:
    """A one-line summary of a tool call's arguments — the path, the query, the
    command. This is the difference between "read_file" and "read_file
    src/daimon_agent/graph.py" in the transcript."""
    if not isinstance(args, dict):
        return None
    for key in _DETAIL_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text[: limit - 1] + "…" if len(text) > limit else text
    return None


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


def sanitize_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Make a conversation structurally valid for the model API.

    The API enforces a strict pairing: every tool_call gets exactly one
    response, and every tool response answers a call that came before it.
    Three ways a history breaks that, all of which happen in practice:

    - **Unanswered calls.** The process was killed mid-turn, so the tool
      results never landed. Rejected as "insufficient tool messages following
      tool_calls message". Repaired by injecting a synthetic result.
    - **Orphaned responses.** Compaction dropped the AIMessage but kept its
      results — the cut has to land somewhere. Rejected as "must be a response
      to a preceding message with 'tool_calls'". Repaired by dropping them.
    - **Duplicate responses.** Two results for one call id. Same rejection.
      Repaired by keeping the first.
    - **Malformed calls.** The model emitted unparseable arguments, so the call
      landed in `invalid_tool_calls` rather than `tool_calls` and the tools node
      never answered it. Repaired the same way as an unanswered call, because
      the API cannot tell the two apart.

    This runs before every model call *and* before compaction persists its
    result. The second one matters more than it looks: compaction now rewrites
    stored history, so an invalid list isn't one bad request — it is a session
    that can never make a valid request again.
    """
    repaired: list[AnyMessage] = []
    # tool_call_ids opened by the last AIMessage that nothing has answered yet.
    # A dict rather than a set so synthetic results come out in call order.
    open_calls: dict[str, None] = {}

    def close_batch() -> None:
        """Answer whatever the last AIMessage left hanging. Called when the run
        of tool responses ends, so synthetics land *after* the real ones."""
        for call_id in open_calls:
            repaired.append(
                ToolMessage(
                    content=(
                        "(This tool call was interrupted — the agent process was "
                        "killed or restarted before it could complete.)"
                    ),
                    tool_call_id=call_id,
                    name="unknown",
                )
            )
        open_calls.clear()

    for msg in messages:
        if isinstance(msg, ToolMessage):
            # Keep it only if it answers a call that is open. Anything else is
            # an orphan or a duplicate, and either makes the request invalid.
            if msg.tool_call_id in open_calls:
                del open_calls[msg.tool_call_id]
                repaired.append(msg)
            continue

        close_batch()
        repaired.append(msg)
        if isinstance(msg, AIMessage) and (msg.tool_calls or msg.invalid_tool_calls):
            # invalid_tool_calls count as open calls. The OpenAI-compatible
            # serializer emits them into the request's `tool_calls` array
            # alongside the valid ones, so a stored AIMessage carrying one that
            # nothing answered makes every future request in the session
            # invalid — not just the turn it happened on.
            open_calls = {
                call["id"]: None
                for call in [*msg.tool_calls, *msg.invalid_tool_calls]
                if call.get("id")
            }

    close_batch()
    return repaired


#: Old name, kept because the behaviour it described is a subset of this one.
_repair_orphaned_tool_calls = sanitize_messages


def with_prompt_cache(system_prompt: str, messages: list[AnyMessage]) -> list[AnyMessage]:
    """The model-call message list with explicit cache breakpoints.

    Anthropic caches everything *before* a `cache_control` marker, and orders a
    request tools → system → messages. So two markers cover the two things that
    are stable and expensive:

    - **On the system prompt**, which caches the tool schemas with it. That is
      the single largest fixed cost in every request — forty-odd schemas plus
      the rules text, re-sent on every hop of every turn.
    - **On the newest user instruction**, which caches the whole conversation
      that precedes it. That prefix only ever grows, so each turn re-reads what
      the last one paid to write.

    Not marked: the tool results accumulating *after* the instruction within a
    turn. A rolling marker there would cache those too, but it means writing
    `cache_control` into tool_result blocks, and the extra write on every hop
    is not obviously cheaper than the read it saves.

    DeepSeek never comes through here — it caches its prefix automatically,
    which is what `prompts.py`'s frozen-prefix layout is built for. Marking a
    provider that doesn't want markers is how you get a 400.
    """
    out: list[AnyMessage] = [
        SystemMessage(
            content=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        ),
        *messages,
    ]
    for index in range(len(out) - 1, 0, -1):
        message = out[index]
        # String content only: a message already carrying content blocks has
        # been shaped by something else, and re-wrapping it would drop whatever
        # that was.
        if isinstance(message, HumanMessage) and isinstance(message.content, str):
            out[index] = HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": message.content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                id=message.id,
            )
            break
    return out


def _chunk_reasoning(chunk: Any) -> str:
    """The model's own reasoning text on a chunk, across provider spellings:
    DeepSeek puts `reasoning_content` in additional_kwargs; Anthropic emits
    thinking content blocks."""
    extra = getattr(chunk, "additional_kwargs", None) or {}
    text = extra.get("reasoning_content") or extra.get("reasoning")
    if isinstance(text, str):
        return text
    content = getattr(chunk, "content", None)
    if isinstance(content, list):
        parts = [
            str(block.get("thinking", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        return "".join(parts)
    return ""


def _chunk_text(chunk: Any) -> str:
    """The answer text on a chunk, ignoring non-text content blocks."""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts)
    return ""


def _as_message(final: AIMessageChunk) -> AIMessage:
    """Convert the accumulated chunk into a plain AIMessage for the state.
    Storing chunks would work but leaks a streaming detail into the
    checkpointed history that every reader would then have to handle."""
    return AIMessage(
        content=final.content,
        tool_calls=list(final.tool_calls),
        invalid_tool_calls=list(final.invalid_tool_calls),
        additional_kwargs=dict(final.additional_kwargs),
        response_metadata=dict(final.response_metadata),
        usage_metadata=final.usage_metadata,
        id=final.id,
    )


#: Attempts per model call, and the backoff between them. Deliberately small:
#: this covers a blip — a reset connection, a 503, a rate limit — not an
#: outage. The provider client's own `max_retries` only covers establishing the
#: request; a stream that dies after the first chunk is ours to handle.
STREAM_ATTEMPTS = 3
STREAM_BACKOFF_S = (1.0, 2.0)


def _status_code(exc: BaseException) -> int | None:
    """The HTTP status behind a provider exception, wherever it hides."""
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    value = getattr(getattr(exc, "response", None), "status_code", None)
    return value if isinstance(value, int) else None


#: Exception family names that mean "the network misbehaved". Matched by name
#: rather than by type because the real classes live in httpx, openai, and
#: anthropic — and a DeepSeek-only install must not have to import anthropic's
#: exception tree to decide whether to retry.
_TRANSIENT_MARKERS = (
    "timeout", "connection", "connect", "remoteprotocol", "readerror",
    "internalserver", "serviceunavailable", "overloaded", "ratelimit",
)


def _is_transient(exc: BaseException) -> bool:
    """Whether a failed model call is worth retrying.

    Status first, because it is unambiguous: 429 and 5xx are the provider
    asking us to come back later, while every other 4xx is our own request
    being wrong and will fail identically forever. Only with no status to go on
    do we fall back to the exception's family name.
    """
    status = _status_code(exc)
    if status is not None:
        return status == 429 or 500 <= status < 600
    name = type(exc).__name__.lower()
    return any(marker in name for marker in _TRANSIENT_MARKERS)


async def stream_model(
    model: Any,
    messages: list[AnyMessage],
    *,
    emit_deltas: bool = True,
    agent_id: str | None = None,
) -> AIMessage:
    """Run one model call, streaming its output to the event bus.

    Chunks accumulate with `+`, which is what correctly reassembles tool calls
    arriving as fragments across chunks. `emit_deltas` is off for sub-agents:
    two agents' narration interleaved in one transcript is noise, and the UI
    shows their tool steps instead.

    A transient failure restarts the whole stream rather than ending the turn.
    That matters more than it sounds: the exception used to propagate out of the
    agent node and close the turn with an `error` event, throwing away however
    many steps of real work came before it. Restarting is safe because the
    request is a pure function of `messages`, which this call never mutates.
    """
    emitted = False

    async def attempt(*, deltas: bool) -> AIMessage:
        nonlocal emitted
        final: AIMessageChunk | None = None
        async for chunk in model.astream(messages):
            final = chunk if final is None else final + chunk
            if not deltas:
                continue
            reasoning = _chunk_reasoning(chunk)
            if reasoning:
                emitted = True
                emit(assistant_delta_event(reasoning, channel="reasoning", agent_id=agent_id))
            text = _chunk_text(chunk)
            if text:
                emitted = True
                emit(assistant_delta_event(text, agent_id=agent_id))
        if final is None:  # a model that yielded nothing at all
            return AIMessage(content="")
        return _as_message(final)

    for index in range(STREAM_ATTEMPTS):
        try:
            # Stay silent on a retry only if the dead attempt already wrote to
            # the transcript — re-streaming a paragraph the user just watched
            # appear reads as the agent stuttering. A stream that died before
            # emitting anything has nothing to duplicate, so it keeps streaming.
            return await attempt(deltas=emit_deltas and not emitted)
        except Exception as exc:
            if index == STREAM_ATTEMPTS - 1 or not _is_transient(exc):
                raise
            await asyncio.sleep(STREAM_BACKOFF_S[index])
    raise AssertionError("unreachable: the last attempt either returns or raises")


def build_graph(
    settings: Any,
    router: ModelRouter,
    tools: list[BaseTool],
    *,
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
    is_main = role == "pro"

    # One subgraph per agent type, built once here rather than per spawn.
    # The recursive calls pass role="flash", so a subgraph has no `subagents`
    # node at all — nesting is structurally impossible, not merely discouraged.
    subgraphs: dict[str, CompiledStateGraph] = {}
    if is_main:
        for agent_type, allowed in SUBAGENT_TOOLSETS.items():
            subset = [
                t
                for t in tools
                if t.name not in NON_DELEGABLE_TOOLS
                and (allowed is None or t.name in allowed)
            ]
            subgraphs[agent_type] = build_graph(settings, router, subset, role="flash")

    def _visible_tools(state: AgentState) -> list[BaseTool]:
        """The tools this turn may use. Asking is gated on the client having
        said it can answer — an agent that asks a question nobody will ever
        answer has hung, not paused."""
        if "ask" in (state.get("capabilities") or []):
            return tools
        return [t for t in tools if t.name not in ASK_TOOLS]

    async def agent_node(state: AgentState) -> dict:
        # Lazy: the model is constructed on the first call, not at graph-build
        # time, so a keyless process can still boot and serve /health. The
        # router caches instances; bind_tools per node call is cheap.
        visible = _visible_tools(state)
        model = router.for_role(role, streaming=True).bind_tools(visible)
        _build_prompt = prompt_builder or build_system_prompt
        system_prompt = _build_prompt(
            settings,
            skills_block=state.get("skills_block", ""),
            mode=state.get("mode", "normal"),
        )

        update: dict = {}
        # Compaction (token-threshold summarization via flash) applies only to
        # the main agent — subgraph turns are bounded by their own recursion.
        compaction = (
            await compact_if_needed(settings, router, state["messages"])
            if is_main
            else None
        )
        if compaction is not None:
            # Sanitize *before* persisting, not just before the call: the cut
            # can land inside a tool batch and orphan its results, and a
            # compacted history is stored, so an invalid one would break every
            # future turn in this session rather than a single request.
            compaction.messages = sanitize_messages(compaction.messages)
            compaction.replacement = sanitize_messages(compaction.replacement)
            messages = list(compaction.messages)
            # Persist it, so the next hop starts from the compacted history
            # instead of re-summarizing the same prefix.
            update["messages"] = compaction.as_update()
            emit(
                compaction_event(
                    compaction.before_tokens, compaction.after_tokens, compaction.dropped
                )
            )
        else:
            messages = list(state["messages"])

        # Last line of defence before the request goes out.
        messages = sanitize_messages(messages)
        if supports_prompt_cache_control(router.spec_for_role(role), settings.provider):
            messages = with_prompt_cache(system_prompt, messages)
        else:
            messages = [SystemMessage(content=system_prompt), *messages]

        response = await stream_model(
            model,
            messages,
            emit_deltas=is_main,
            agent_id=state.get("agent_id"),
        )
        # Close the streamed block. Without this the preamble before a tool
        # call ("Let me read the file first") runs straight into whatever the
        # next model call writes, since neither ends with a newline of its own.
        if is_main and _chunk_text(response).strip():
            emit(assistant_delta_event("\n", agent_id=state.get("agent_id")))

        # Usage: the main agent's input count is also the live context size.
        # The message count goes with it, so compaction can tell how much has
        # been appended since and compact *before* the next request rather than
        # after it has already been paid for. The system message is excluded —
        # `compact_if_needed` is handed the bare conversation.
        if is_main:
            note_context_messages(len(messages) - 1)
        call_usage = extract_usage(response, fallback_model=router.model_name(role))
        record(call_usage, is_context=is_main)
        if call_usage is not None:
            emit(
                usage_event(
                    call_usage.model,
                    call_usage.input_tokens,
                    call_usage.output_tokens,
                    cache_read_tokens=call_usage.cache_read_tokens,
                    cache_write_tokens=call_usage.cache_write_tokens,
                    cost_usd=call_usage.cost_usd(),
                    role=role,
                    agent_id=state.get("agent_id"),
                )
            )

        for call in response.tool_calls:
            name = call.get("name", "tool")
            emit(
                step_event(
                    call["id"],
                    name,
                    "running",
                    name,
                    detail=_call_detail(call.get("args") or {}),
                    agent_id=state.get("agent_id"),
                )
            )

        update["messages"] = [*update.get("messages", []), response]
        return update

    async def tools_node(state: AgentState) -> dict:
        last: AIMessage = state["messages"][-1]
        # Sparse while a batch is open: a scheduled read reserves its slot with
        # None and drain() fills it in.
        results: list[ToolMessage | None] = []
        pending: list[tuple[str, str, str, str, str]] = list(state.get("research_pending", []))
        research_used = state.get("research_used", 0)
        call_log = list(state.get("call_log", []))
        last_sig = state.get("last_read_signature")
        agent_id = state.get("agent_id")

        # Consecutive concurrent-safe calls accumulate here and run together.
        # Each entry is (slot, call_id, name, args, tool, detail); `slot` is the
        # index in `results` reserved for its answer, so the batch can finish in
        # any order and still rejoin the conversation in the order the model
        # asked for.
        batch: list[tuple[int, str, str, dict, BaseTool, str | None]] = []

        async def execute(
            call_id: str, name: str, args: dict, tool: BaseTool, detail: str | None
        ) -> tuple[str, ToolMessage | None]:
            """Run one tool. Returns (content, None) on success, or
            ("", error_message) when it raised — a tool failure is a message the
            model can act on, not a crash that ends the turn."""
            started = time.monotonic()
            try:
                content = clip_result(_tool_result_text(await tool.ainvoke(args)))
            except Exception as exc:
                emit(
                    step_event(
                        call_id, name, "error", name,
                        detail=detail,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        agent_id=agent_id,
                    )
                )
                return "", ToolMessage(
                    content=f"{name} failed: {exc}"[:2000],
                    tool_call_id=call_id,
                    name=name,
                    status="error",
                )
            emit(
                step_event(
                    call_id, name, "done", name,
                    detail=detail,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    agent_id=agent_id,
                )
            )
            return content, None

        async def drain() -> None:
            """Run the open batch concurrently and fill in its reserved slots."""
            if not batch:
                return
            # Snapshot and clear in one step, so a tool that somehow scheduled
            # more work can't be drained twice. Named `open_batch` rather than
            # `pending` to stay clear of the research fan-out list of that name.
            open_batch, batch[:] = list(batch), []

            async def run_one(entry: tuple) -> ToolMessage:
                _slot, call_id, name, args, tool, detail = entry
                content, failure = await execute(call_id, name, args, tool, detail)
                if failure is not None:
                    return failure
                return ToolMessage(content=content, tool_call_id=call_id, name=name)

            for entry, message in zip(
                open_batch, await asyncio.gather(*map(run_one, open_batch))
            ):
                results[entry[0]] = message

        # --- questions first, before anything executes ---------------------
        # interrupt() suspends the graph here; resuming re-runs this node from
        # the top, and the already-answered interrupts replay their recorded
        # values while the next unanswered one suspends again. Resolving them
        # ahead of the execution loop is what makes that re-entry safe: if a
        # tool had already run, the re-run would run it a second time.
        #
        # The capability is re-checked here and not only at bind time: a model
        # that names a tool it wasn't offered would otherwise suspend the turn
        # on a question no client is listening for, which looks like a hang.
        ask_enabled = "ask" in (state.get("capabilities") or [])
        plan_approved = bool(state.get("plan_approved"))
        gate_plan = state.get("mode") == "plan" and ask_enabled and not plan_approved
        answers: dict[str, Any] = {}
        for call in last.tool_calls:
            if call.get("name") not in ASK_TOOLS:
                continue
            if not ask_enabled:
                continue
            answers[call.get("id", "")] = interrupt(
                _ask_payload(call.get("name", ""), call.get("args") or {})
            )
            # An answered plan unlocks the mutating tools for the rest of the
            # turn. Rejecting it ("Revise it") leaves the gate shut, so a
            # revised plan has to be approved on its own terms.
            if call.get("name") == "present_plan" and _is_approval(
                answers[call.get("id", "")]
            ):
                plan_approved = True
                gate_plan = False

        # --- malformed calls, answered before anything runs -----------------
        # The model named a tool but its arguments didn't parse, so LangChain
        # put the call in `invalid_tool_calls` and it has no `args` to execute.
        # It still needs a ToolMessage: the API counts it as an open call, and
        # an unanswered one is stored and re-sent on every later turn.
        for bad in getattr(last, "invalid_tool_calls", None) or []:
            call_id = bad.get("id") or f"call_{len(results)}"
            name = bad.get("name") or "tool"
            results.append(
                ToolMessage(
                    content=MALFORMED_CALL.format(
                        tool=name, error=bad.get("error") or "unparseable arguments"
                    ),
                    tool_call_id=call_id,
                    name=name,
                    status="error",
                )
            )
            emit(step_event(call_id, name, "error", name, agent_id=agent_id))

        for call in last.tool_calls:
            name = call.get("name", "")
            args = call.get("args") or {}
            tool = tool_by_name.get(name)
            call_id = call.get("id", f"call_{len(results)}")
            detail = _call_detail(args)

            # --- the user's answer, already collected above -----------------
            if name in ASK_TOOLS:
                results.append(
                    ToolMessage(
                        content=(
                            _format_answer(answers.get(call_id))
                            if ask_enabled
                            else (
                                f"{name} is not available here — this session has no way to "
                                f"reach the user mid-turn. Decide it yourself, state the "
                                f"assumption you made, and continue."
                            )
                        ),
                        tool_call_id=call_id,
                        name=name,
                        **({} if ask_enabled else {"status": "error"}),
                    )
                )
                emit(
                    step_event(
                        call_id, name, "done" if ask_enabled else "error", name,
                        agent_id=agent_id,
                    )
                )
                continue

            # --- plan gate (never executes without an approved plan) --------
            if gate_plan and name in MUTATING_TOOLS:
                results.append(
                    ToolMessage(
                        content=PLAN_REQUIRED.format(tool=name),
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                emit(step_event(call_id, name, "error", name, detail=detail, agent_id=agent_id))
                continue

            # --- guardrail pre-check (never executes on a warning) ----------
            # The budget and near-duplicate checks are research-specific. The
            # exact-repeat check is not: an agent stuck on an edit_file whose
            # old_text never matches will reissue it verbatim until the step
            # budget runs out, and nothing used to stop it.
            warning: str | None = None
            if name in RESEARCH_TOOLS:
                warning = check_research_budget(name, research_used)
                if name == "web_search":
                    warning = warning or check_near_duplicate(str(args.get("query", "")), call_log)
            if warning is None and name not in REPEAT_EXEMPT:
                warning = check_repeat(name, args, call_log)
            if warning:
                results.append(ToolMessage(content=warning, tool_call_id=call_id, name=name))
                emit(step_event(call_id, name, "done", name, detail=detail, agent_id=agent_id))
                continue

            # --- fan-out: delegated to subagents, not executed here ---------
            if name in ("research", "task"):
                if name == "research":
                    # `research` fans out: one line, one sub-agent.
                    queries = [
                        q.strip()
                        for q in str(args.get("queries", "")).splitlines()
                        if q.strip()
                    ][:MAX_RESEARCH_FAN_OUT]
                    agent_type = "research"
                else:
                    # `task` is one job per call, multi-line prompt and all.
                    # Concurrency comes from the model issuing several `task`
                    # calls in one message — they all land in `pending` and the
                    # subagent node gathers them together.
                    prompt = str(args.get("prompt", "")).strip()
                    queries = [prompt] if prompt else []
                    agent_type = str(args.get("agent_type", DEFAULT_AGENT_TYPE)).strip().lower()
                    if agent_type not in SUBAGENT_TOOLSETS:
                        agent_type = DEFAULT_AGENT_TYPE
                if len(pending) >= MAX_SUBAGENT_FAN_OUT:
                    results.append(
                        ToolMessage(
                            content=(
                                f"{name}: already spawning {len(pending)} sub-agents this "
                                f"step (the cap is {MAX_SUBAGENT_FAN_OUT}). Wait for these "
                                f"to report back before spawning more."
                            ),
                            tool_call_id=call_id,
                            name=name,
                        )
                    )
                    emit(step_event(call_id, name, "done", name, agent_id=agent_id))
                    continue
                if not queries:
                    results.append(
                        ToolMessage(
                            content=(
                                f"{name}: no queries found — put one research question per line."
                                if name == "research"
                                else f"{name}: prompt is required."
                            ),
                            tool_call_id=call_id,
                            name=name,
                        )
                    )
                    emit(step_event(call_id, name, "done", name, agent_id=agent_id))
                    continue
                for i, query in enumerate(queries):
                    pending.append((call_id, f"{call_id}-{i}", name, query, agent_type))
                # One budget slot for the fan-out, like any other research call.
                research_used += 1
                call_log.append((name, json.dumps(args, sort_keys=True, default=str)))
                continue

            if tool is None:
                message = f'Tool "{name}" is not available in this session.'
                results.append(
                    ToolMessage(content=message, tool_call_id=call_id, name=name, status="error")
                )
                emit(step_event(call_id, name, "error", name, detail=detail, agent_id=agent_id))
                continue

            # --- concurrent-safe reads: scheduled, not run here -------------
            # They join the open batch and execute together at the next drain.
            # The repeat guard sees this call logged immediately, so two
            # identical reads in one batch are still caught.
            if name in CONCURRENT_SAFE_TOOLS:
                call_log.append((name, json.dumps(args, sort_keys=True, default=str)))
                results.append(None)  # placeholder; drain() fills this slot
                batch.append((len(results) - 1, call_id, name, args, tool, detail))
                continue

            # Everything else runs inline — and the open batch has to land
            # first. A read scheduled before a write must not observe that
            # write, which is exactly what a drain here guarantees and what a
            # partition-everything-then-gather approach would lose.
            await drain()

            content, message = await execute(call_id, name, args, tool, detail)
            if message is not None:  # the tool raised; its error is the answer
                results.append(message)
                continue

            # --- post-execution bookkeeping ---------------------------------
            call_log.append((name, json.dumps(args, sort_keys=True, default=str)))
            # A tool that changed something invalidates the repeat log. Reading
            # a file you just edited, or re-running the suite after a fix, is
            # the correct move rather than a loop — the answer genuinely differs
            # now. Research entries survive: the web didn't change because we
            # wrote a file.
            if name in MUTATING_TOOLS:
                call_log = [row for row in call_log if row[0] in RESEARCH_TOOLS]
            if name in RESEARCH_TOOLS:
                research_used += 1
                if name == "read_page":
                    warning, last_sig = check_page_unchanged(last_sig, content)
                    if warning:
                        # The read executed but returned nothing new — surface
                        # the warning instead of the content, like era-1.
                        content = warning

            results.append(ToolMessage(content=content, tool_call_id=call_id, name=name))

        # Whatever is still open when the calls run out.
        await drain()

        return {
            "messages": results,
            "research_used": research_used,
            "call_log": call_log,
            "last_read_signature": last_sig,
            "research_pending": pending,
            "plan_approved": plan_approved,
        }

    async def subagent_node(state: AgentState) -> dict:
        """Drain the fan-out: one flash-model subgraph run per pending query,
        all of them concurrently.

        Concurrency is what makes the emitter wrapper simple. Each sub-agent
        runs in its own asyncio task, and a task starts from a *copy* of the
        current context — so `set_active_emit` inside one is invisible to its
        siblings and to the parent, with no save/restore around it. The spawn
        and completion events deliberately go through the captured parent emit
        so they land at the top level rather than nested under themselves.
        """
        parent_emit = _active.get()
        pending = list(state.get("research_pending", []))

        async def run_one(entry: tuple) -> ToolMessage:
            call_id, sub_id, name, query, agent_type = entry
            label = f"{agent_type}: {query[:60]}"
            started = time.monotonic()
            if parent_emit is not None:
                parent_emit(
                    step_event(
                        sub_id, label, "running",
                        detail=query[:120],
                        agent_id=sub_id,
                        agent_label=agent_type,
                    )
                )

            # Everything the subgraph emits is stamped with its parent and its
            # own agent id, so a UI can group concurrent agents instead of
            # interleaving their steps into one unreadable list.
            def sub_emit(event: dict) -> None:
                if event.get("type") == "step":
                    event["parent_step_id"] = sub_id
                    event["subagent_query"] = query[:80]
                if parent_emit is not None:
                    parent_emit(event)

            set_active_emit(sub_emit)
            status = "done"
            try:
                final = await subgraphs[agent_type].ainvoke(
                    {
                        "messages": [HumanMessage(content=query)],
                        "instruction": query,
                        "session_id": f"subagent-{sub_id}",
                        "agent_id": sub_id,
                        # A subagent never asks the user, whatever the parent's
                        # client can do — it has no channel back.
                        "capabilities": [],
                    },
                    {"recursion_limit": SUBAGENT_RECURSION_LIMIT},
                )
                message = ToolMessage(
                    content=extract_result(final["messages"]), tool_call_id=call_id, name=name
                )
            except Exception as exc:
                message = ToolMessage(
                    content=f"{agent_type} sub-agent failed: {exc}",
                    tool_call_id=call_id,
                    name=name,
                    status="error",
                )
                status = "error"

            if parent_emit is not None:
                parent_emit(
                    step_event(
                        sub_id, name, status, name,
                        detail=query[:120],
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        agent_id=sub_id,
                        agent_label=agent_type,
                    )
                )
            return message

        if not pending:
            return {"messages": [], "research_pending": []}
        # gather preserves input order, so results rejoin the conversation in
        # the order the model asked for them regardless of who finished first.
        results = await asyncio.gather(*(run_one(entry) for entry in pending))

        # One ToolMessage per tool_call, however many sub-agents that call
        # spawned. A `research` call with three queries is still *one* tool
        # call, and answering it three times produces a conversation the API
        # rejects outright ("must be a response to a preceding message with
        # tool_calls") — the extra answers correspond to nothing.
        merged: dict[str, list[tuple[str, ToolMessage]]] = {}
        for (call_id, _sub_id, _name, query, _agent_type), message in zip(pending, results):
            merged.setdefault(call_id, []).append((query, message))

        out: list[ToolMessage] = []
        for call_id, parts in merged.items():
            if len(parts) == 1:
                out.append(parts[0][1])
                continue
            # Label each finding with the question that produced it, or the
            # model has several answers and no way to tell them apart.
            body = "\n\n".join(
                f"[{query}]\n{message.content}" for query, message in parts
            )
            out.append(
                ToolMessage(
                    content=body,
                    tool_call_id=call_id,
                    name=parts[0][1].name,
                    status=(
                        "error"
                        if all(m.status == "error" for _, m in parts)
                        else "success"
                    ),
                )
            )
        return {"messages": out, "research_pending": []}

    def route_after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        # invalid_tool_calls route to `tools` as well. They are calls the model
        # meant to make and got wrong; ending the turn here would leave them
        # unanswered in the stored history, which poisons the session.
        if getattr(last, "tool_calls", None) or getattr(last, "invalid_tool_calls", None):
            return "tools"
        return END

    def route_after_tools(state: AgentState) -> str:
        if state.get("research_pending"):
            return "subagents"
        return "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    if subgraphs:
        graph.add_node("subagents", subagent_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", END: END})
    if subgraphs:
        graph.add_conditional_edges(
            "tools", route_after_tools, {"subagents": "subagents", "agent": "agent"}
        )
        graph.add_edge("subagents", "agent")
    else:
        graph.add_edge("tools", "agent")
    compiled = graph.compile(checkpointer=checkpointer)
    compiled.tools = tools  # attached for the /tools HTTP endpoint
    return compiled


# --- ask payloads ------------------------------------------------------------

def _ask_payload(name: str, args: dict) -> dict:
    """The interrupt value for an ask tool — the same shape the `ask` event
    carries, so run.py can forward it without re-deriving anything."""
    options = parse_options(str(args.get("options", "")))
    if name == "present_plan":
        plan = str(args.get("plan", "")).strip()
        return ask_event(
            str(uuid4()),
            "plan",
            str(args.get("question", "") or "Ready to go with this plan?"),
            options
            or [
                {"label": "Go ahead", "description": "Implement the plan as written."},
                {"label": "Revise it", "description": "I'll say what to change."},
            ],
            plan=plan,
            header="Plan",
        )
    return ask_event(
        str(uuid4()),
        "question",
        str(args.get("question", "")).strip(),
        options,
        multi_select=str(args.get("multi_select", "")).strip().lower()
        in ("1", "true", "yes", "on"),
        header=str(args.get("header", "") or "") or None,
    )


#: Answers that mean "yes, do it". Anything else — including a written-out
#: revision — leaves the plan gate shut, which is the safe direction to err in.
_APPROVALS = ("go ahead", "approve", "yes", "proceed", "do it", "looks good", "ok", "okay")


def _is_approval(answer: Any) -> bool:
    if isinstance(answer, list):
        return any(_is_approval(a) for a in answer)
    text = str(answer or "").strip().lower()
    return any(text.startswith(word) or text == word for word in _APPROVALS)


def _format_answer(answer: Any) -> str:
    """The user's answer as a tool result. Declining is stated plainly rather
    than left as an empty string the model has to interpret."""
    if answer is None:
        return "The user did not answer."
    if isinstance(answer, list):
        return (
            "The user chose: " + ", ".join(str(a) for a in answer)
            if answer
            else "The user did not answer."
        )
    text = str(answer).strip()
    return f"The user answered: {text}" if text else "The user did not answer."


def run_config(session_id: str, recursion_limit: int | None = None) -> dict:
    """The config every turn runs with: checkpointer thread + recursion cap.

    The cap guards against infinite loops; it is not a budget for how long a
    task may take. `run.py` catches it and continues from the checkpoint, and
    enforces the real ceiling (`max_steps_per_turn`) itself.
    """
    return {
        "configurable": {"thread_id": session_id},
        "recursion_limit": recursion_limit or RECURSION_LIMIT,
    }


def extract_result(messages: list[AnyMessage]) -> str:
    """The last non-blank assistant text, else the legacy fallback. Like
    legacy's `.trim()`, whitespace-only results are treated as empty."""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content:
            text = _tool_result_text(message.content)
            if text.strip():
                return text.strip()
    return "Task complete."
