"""Turn-scoped event emission — port of `legacy/agents/src/emitter.ts`.

A ContextVar (not a module global like legacy) so that turns on separate
asyncio tasks never cross their events; `emit()` is a silent no-op when no
turn is active. The graph nodes call `emit()` from inside LangGraph tasks,
which inherit this context from the caller of `run_turn`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

Emit = Callable[[dict], None]

_active: ContextVar[Emit | None] = ContextVar("daimon_active_emit", default=None)


def set_active_emit(fn: Emit | None) -> None:
    _active.set(fn)


def emit(event: dict) -> None:
    fn = _active.get()
    if fn is not None:
        fn(event)
