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
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

import hashlib

from .emitter import set_active_emit
from .events import ask_event, continuation_event, done_event, error_event, step_event
from .graph import extract_result, run_config
from .reflect import reflect_turn, should_reflect
from .skills.injector import format_skills_index, select_skills
from .usage import UsageAccumulator, get_active_usage, set_active_usage


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


def _continue_ask(steps: int, max_steps: int) -> dict:
    """The 'keep going?' prompt raised when a turn exhausts its step budget.

    Deliberately the same `ask` shape the agent's own questions use, so the UI
    needs no second mechanism. It is *not* a graph interrupt though — nothing
    is suspended — which is how `resume_turn` tells the two apart.
    """
    return ask_event(
        str(uuid4()),
        "continue",
        f"Still working after {steps} steps — keep going?",
        [
            {"label": "Continue", "description": f"another {max_steps} steps"},
            {"label": "Stop here", "description": "report what's done so far"},
        ],
        header="Budget",
    )


async def _drive(
    payload: Any,
    session_id: str,
    settings: Any,
    emit: Callable[[dict], None],
    *,
    graph: Any,
    thinking_id: str,
    can_ask: bool = False,
    steps: int = 0,
) -> tuple[str | None, dict | None]:
    """Run the graph to its next stopping point.

    Returns `(result, ask)` — exactly one is set. `payload` is the initial
    state for a new turn, or a `Command` for a resume; the graph doesn't care
    which, and neither does anything below this line.

    The graph's recursion limit is a guard against infinite loops, not a budget
    for how much work a task may take — a real project runs through it several
    times over. Hitting it used to surface as an error the user had to answer by
    re-prompting; instead we continue from the checkpoint, which is where the
    state already was. `max_steps_per_turn` is the ceiling that actually bounds
    an unattended run.
    """
    limit = settings.recursion_limit
    config = run_config(session_id, limit)

    while True:
        stream = graph.astream(payload, config, stream_mode="updates")
        try:
            await _stream_with_inactivity_timeout(stream, settings.inactivity_timeout_s)
        except GraphRecursionError:
            steps += limit
            if steps >= settings.max_steps_per_turn:
                if not can_ask:
                    # Nobody to answer — stop with whatever was accomplished
                    # rather than park on a question no one will see.
                    break
                return None, _continue_ask(steps, settings.max_steps_per_turn)
            emit(continuation_event(steps, settings.max_steps_per_turn, _turn_tokens()))
            # None resumes the pending task from the checkpoint. No nudge
            # message: the model simply carries on, and the conversation stays
            # free of scaffolding it would otherwise have to read every turn.
            payload = None
            continue

        state = await graph.aget_state(config)
        ask = _pending_ask(state)
        if ask is not None:
            # Not an ending — the thinking indicator stays open across the
            # pause, because the turn genuinely is still in flight.
            return None, ask
        break

    state = await graph.aget_state(config)
    emit(step_event(thinking_id, "Thinking", "done"))
    return extract_result(state.values.get("messages", [])), None


def _turn_tokens() -> int:
    acc = get_active_usage()
    return acc.total_tokens if acc is not None else 0


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
        # Skills tail: every skill as one name + description line, appended
        # after the frozen rules prefix. The agent reads a body with read_skill
        # when one actually applies (see injector.format_skills_index).
        skills_block = format_skills_index(select_skills(instruction, skills)) if skills else ""
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
            can_ask="ask" in (capabilities or []),
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
        # Two things end a turn in a way that can be resumed, and they resume
        # differently. A graph `interrupt()` leaves a suspended node, and
        # `Command(resume=...)` feeds the answer back into it. A continuation
        # ask suspends nothing — the graph merely ran out of steps — so it
        # continues with `None`. The state itself tells them apart, which
        # means the client doesn't have to.
        config = run_config(session_id, settings.recursion_limit)
        suspended = _pending_ask(await graph.aget_state(config)) is not None

        if suspended:
            payload: Any = Command(resume=answer)
        elif _means_stop(answer):
            # Declining a continuation: wrap up with what's been done rather
            # than running further.
            state = await graph.aget_state(config)
            emit(step_event(thinking_id, "Thinking", "done"))
            result = extract_result(state.values.get("messages", []))
            emit(done_event(result, usage=usage.totals()))
            return result
        else:
            payload = None

        result, ask = await _drive(
            payload,
            session_id,
            settings,
            emit,
            graph=graph,
            thinking_id=thinking_id,
            can_ask=True,  # they just answered one, so they can answer another
        )
        if ask is not None:
            emit(ask)
            return None
        emit(done_event(result or "", usage=usage.totals()))
        # The instruction for bookkeeping is the conversation's own first
        # human message, which the checkpointer still has — reconstructing it
        # here beats making the client echo it back.
        state = await graph.aget_state(config)
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


#: Answers to a continuation ask that mean "wrap up". Anything else continues,
#: which is the safe default here — the cost of one more segment is small next
#: to abandoning work the user asked for.
_STOP_WORDS = ("stop", "no", "halt", "cancel", "done", "quit", "enough")


def _means_stop(answer: Any) -> bool:
    if isinstance(answer, list):
        return any(_means_stop(a) for a in answer)
    text = str(answer or "").strip().lower()
    return any(text == word or text.startswith(word + " ") for word in _STOP_WORDS)


def _first_human(messages: list) -> str | None:
    """The most recent user instruction — what this turn was actually about."""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return None
