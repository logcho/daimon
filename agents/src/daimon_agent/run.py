"""The turn loop — port of `legacy/agents/src/run.ts`.

Per-turn sequence (the contract `tests/test_events.py` freezes):
step(Thinking, running) -> per tool step(id, label, running) ...
step(id, label, done|error) -> step(Thinking, done) -> done|error|ask (exclusive).

A turn can end three ways. `done` and `error` are endings. `ask` is a pause:
the graph hit an `interrupt()` and is parked in the checkpointer waiting for an
answer, which arrives via `resume_turn`. From the caller's side all three close
the stream identically, which is why they share the terminal slot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.types import Command

import hashlib

from .emitter import set_active_emit
from .events import done_event, error_event, step_event
from .graph import extract_result, run_config
from .reflect import reflect_turn, should_reflect
from .skills.injector import format_skills_block, select_skills
from .usage import UsageAccumulator, set_active_usage


class TurnTimeoutError(TimeoutError):
    pass


async def _stream_with_inactivity_timeout(stream: Any, timeout_s: float) -> None:
    """Race each streamed chunk against the inactivity timeout, like legacy's
    per-`next()` race: a long-but-progressing turn is fine as long as
    *something* keeps arriving; only true silence trips."""
    iterator = stream.__aiter__()
    while True:
        try:
            await asyncio.wait_for(anext(iterator), timeout_s)
        except asyncio.TimeoutError:
            raise TurnTimeoutError(f"agent produced no output for {timeout_s:g}s") from None
        except StopAsyncIteration:
            return


def _pending_ask(state: Any) -> dict | None:
    """The ask payload the graph is suspended on, if any.

    LangGraph surfaces an interrupt as a pending task with `interrupts` on it,
    rather than by raising — `astream` simply finishes early. The value is
    whatever `interrupt()` was called with, which the graph builds as a
    ready-made `ask` event so nothing has to be re-derived here.
    """
    for task in getattr(state, "tasks", ()) or ():
        for interrupt in getattr(task, "interrupts", ()) or ():
            value = getattr(interrupt, "value", None)
            if isinstance(value, dict):
                return value
    return None


async def _drive(
    payload: Any,
    session_id: str,
    settings: Any,
    emit: Callable[[dict], None],
    *,
    graph: Any,
    thinking_id: str,
) -> tuple[str | None, dict | None]:
    """Run the graph to its next stopping point.

    Returns `(result, ask)` — exactly one is set. `payload` is the initial
    state for a new turn, or a `Command` for a resume; the graph doesn't care
    which, and neither does anything below this line.
    """
    config = run_config(session_id)
    stream = graph.astream(payload, config, stream_mode="updates")
    await _stream_with_inactivity_timeout(stream, settings.inactivity_timeout_s)

    state = await graph.aget_state(config)
    ask = _pending_ask(state)
    if ask is not None:
        # Not an ending — the thinking indicator stays open across the pause,
        # because the turn genuinely is still in flight.
        return None, ask
    emit(step_event(thinking_id, "Thinking", "done"))
    return extract_result(state.values.get("messages", [])), None


async def _finish(
    instruction: str,
    result: str,
    session_id: str,
    settings: Any,
    *,
    graph: Any,
    memory: Any,
    router: Any,
) -> None:
    """Post-`done` bookkeeping: record the task, then reflect.

    Reflection runs after the user already has their result and only on
    tool-using turns; a failed or gated reflection never fails the turn.
    Awaiting keeps one writer to the memory DB and makes the behavior
    deterministic — the daemon can move it off-thread later.
    """
    if memory is None:
        return
    memory.record_task(instruction, result, "done")
    if router is None or not getattr(settings, "reflect", True):
        return
    try:
        state = await graph.aget_state(run_config(session_id))
        messages = state.values.get("messages", [])
        if should_reflect(instruction, result, messages):
            note = await reflect_turn(router, instruction, result)
            if note is not None:
                digest = hashlib.sha1(instruction.encode("utf-8")).hexdigest()[:10]
                memory.index_note(f"reflect/{digest}.md", note)
    except Exception:
        pass  # reflection is advisory — never fail the turn for it


async def run_turn(
    instruction: str,
    session_id: str,
    settings: Any,
    emit: Callable[[dict], None],
    *,
    graph: Any,
    memory: Any = None,
    skills: Any = None,  # list[Skill] — ranked and injected per turn
    router: Any = None,  # for the post-turn reflection pass
    live_frames: Callable[[Callable[[dict], None]], Any] | None = None,
    capabilities: list[str] | None = None,
    mode: str = "normal",
) -> str | None:
    """Run one turn and stream TaskEvents. Returns the final result string, or
    None when the turn errored or paused on a question (the terminal event
    carries the detail either way)."""
    set_active_emit(emit)
    usage = UsageAccumulator()
    set_active_usage(usage)

    thinking_id = str(uuid4())
    emit(step_event(thinking_id, "Thinking", "running"))

    live_frame_task = None
    if live_frames is not None:
        try:
            live_frame_task = live_frames(emit)
        except Exception:
            live_frame_task = None  # best-effort: never fail a turn for frames

    try:
        # Skills tail, appended after the frozen rules prefix (injector ranks
        # by token overlap against the instruction; empty block when none hit).
        skills_block = format_skills_block(select_skills(instruction, skills)) if skills else ""
        result, ask = await _drive(
            {
                "messages": [HumanMessage(content=instruction)],
                "instruction": instruction,
                "session_id": session_id,
                "skills_block": skills_block,
                "capabilities": list(capabilities or []),
                "mode": mode,
                # Each instruction earns its own approval — a plan approved
                # last turn says nothing about what this one intends to do.
                "plan_approved": False,
                # Per-turn bookkeeping — reset every turn so the research
                # budget, duplicate detection, and page-change detection
                # start fresh. The checkpointer carries these across turns
                # if not overridden here.
                "research_used": 0,
                "call_log": [],
                "last_read_signature": None,
                "research_pending": [],
            },
            session_id,
            settings,
            emit,
            graph=graph,
            thinking_id=thinking_id,
        )
        if ask is not None:
            emit(ask)
            return None
        emit(done_event(result or "", usage=usage.totals()))
        await _finish(
            instruction, result or "", session_id, settings,
            graph=graph, memory=memory, router=router,
        )
        return result
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        emit(error_event(message))
        if memory is not None:
            memory.record_task(instruction, message, "error")
        return None
    finally:
        if live_frame_task is not None:
            live_frame_task.cancel()
        set_active_emit(None)
        set_active_usage(None)


async def resume_turn(
    answer: Any,
    session_id: str,
    settings: Any,
    emit: Callable[[dict], None],
    *,
    graph: Any,
    memory: Any = None,
    router: Any = None,
    live_frames: Callable[[Callable[[dict], None]], Any] | None = None,
) -> str | None:
    """Answer a suspended question and run the turn to its next stopping point.

    The instruction isn't repeated — the checkpointer still holds the whole
    conversation, so `Command(resume=...)` picks up inside the node that
    suspended. Usage restarts from zero for this segment: the totals the client
    already saw are its to keep adding to, and a resumed segment that
    re-reported the whole turn's tokens would double-count.
    """
    set_active_emit(emit)
    usage = UsageAccumulator()
    set_active_usage(usage)

    thinking_id = str(uuid4())
    emit(step_event(thinking_id, "Thinking", "running"))

    live_frame_task = None
    if live_frames is not None:
        try:
            live_frame_task = live_frames(emit)
        except Exception:
            live_frame_task = None

    try:
        result, ask = await _drive(
            Command(resume=answer),
            session_id,
            settings,
            emit,
            graph=graph,
            thinking_id=thinking_id,
        )
        if ask is not None:
            emit(ask)
            return None
        emit(done_event(result or "", usage=usage.totals()))
        # The instruction for bookkeeping is the conversation's own first
        # human message, which the checkpointer still has — reconstructing it
        # here beats making the client echo it back.
        state = await graph.aget_state(run_config(session_id))
        instruction = _first_human(state.values.get("messages", [])) or "(resumed turn)"
        await _finish(
            instruction, result or "", session_id, settings,
            graph=graph, memory=memory, router=router,
        )
        return result
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        emit(error_event(message))
        return None
    finally:
        if live_frame_task is not None:
            live_frame_task.cancel()
        set_active_emit(None)
        set_active_usage(None)


def _first_human(messages: list) -> str | None:
    """The most recent user instruction — what this turn was actually about."""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return None
