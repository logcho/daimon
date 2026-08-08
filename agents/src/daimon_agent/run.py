"""The turn loop — port of `legacy/agents/src/run.ts`.

Per-turn sequence (the contract `tests/test_events.py` freezes):
step(Thinking, running) -> per tool step(id, label, running) ...
step(id, label, done|error) -> step(Thinking, done) -> done|error (exclusive).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage

import hashlib

from .emitter import set_active_emit
from .events import done_event, error_event, step_event
from .graph import extract_result, run_config
from .reflect import reflect_turn, should_reflect
from .skills.injector import format_skills_block, select_skills


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
) -> str | None:
    """Run one turn and stream TaskEvents. Returns the final result string,
    or None when the turn errored (the error event carries the detail)."""
    set_active_emit(emit)

    thinking_id = str(uuid4())
    emit(step_event(thinking_id, "Thinking", "running"))

    live_frame_task = None
    if live_frames is not None:
        try:
            live_frame_task = live_frames(emit)
        except Exception:
            live_frame_task = None  # best-effort: never fail a turn for frames

    try:
        config = run_config(session_id)
        # Skills tail, appended after the frozen rules prefix (injector ranks
        # by token overlap against the instruction; empty block when none hit).
        skills_block = format_skills_block(select_skills(instruction, skills)) if skills else ""
        stream = graph.astream(
            {
                "messages": [HumanMessage(content=instruction)],
                "instruction": instruction,
                "session_id": session_id,
                "skills_block": skills_block,
                # Per-turn bookkeeping — reset every turn so the research
                # budget, duplicate detection, and page-change detection
                # start fresh. The checkpointer carries these across turns
                # if not overridden here.
                "research_used": 0,
                "call_log": [],
                "last_read_signature": None,
            },
            config,
            stream_mode="updates",
        )
        await _stream_with_inactivity_timeout(stream, settings.inactivity_timeout_s)

        state = await graph.aget_state(config)
        result = extract_result(state.values.get("messages", []))
        emit(step_event(thinking_id, "Thinking", "done"))
        emit(done_event(result))
        if memory is not None:
            memory.record_task(instruction, result, "done")
        # Reflection runs after `done` (the user's result is already out) and
        # only on tool-using turns; a failed or gated reflection never fails
        # the turn. Awaiting keeps one writer to the memory DB and makes the
        # behavior deterministic — the daemon can move it off-thread later.
        if router is not None and getattr(settings, "reflect", True) and memory is not None:
            try:
                messages = state.values.get("messages", [])
                if should_reflect(instruction, result, messages):
                    note = await reflect_turn(router, instruction, result)
                    if note is not None:
                        digest = hashlib.sha1(instruction.encode("utf-8")).hexdigest()[:10]
                        memory.index_note(f"reflect/{digest}.md", note)
            except Exception:
                pass  # reflection is advisory — never fail the turn for it
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
