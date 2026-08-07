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

from .emitter import set_active_emit
from .events import done_event, error_event, step_event
from .graph import extract_result, run_config


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
    skills: Any = None,  # skills injector result, wired in Phase D
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
        stream = graph.astream(
            {
                "messages": [HumanMessage(content=instruction)],
                "instruction": instruction,
                "session_id": session_id,
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
