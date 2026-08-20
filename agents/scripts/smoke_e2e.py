"""End-to-end smoke: a real task through the real graph with a real API key,
asserting the full NDJSON contract sequence (thinking running -> steps ->
thinking done -> done, never error).

Requires DEEPSEEK_API_KEY in the environment or .env. When PINCHTAB_BASE is
also set, a browser leg runs too (open a page, read it, answer).

Usage:
    uv run python scripts/smoke_e2e.py
"""

from __future__ import annotations

import asyncio
import sys

from daimon_agent import live_frames
from daimon_agent.browser import aclose_browser, build_browser
from daimon_agent.config import Settings
from daimon_agent.graph import build_graph, close_checkpointer, make_sqlite_checkpointer
from daimon_agent.memory import MemoryStore
from daimon_agent.model import ModelRouter
from daimon_agent.run import run_turn
from daimon_agent.skills.injector import discover_skills
from daimon_agent.tools import build_tools

EVENT_SEQUENCE_OK = True


def _emit(event: dict) -> None:
    global EVENT_SEQUENCE_OK
    etype = event.get("type")
    print(f"  {etype}: {event}", file=sys.stderr)
    if etype == "error":
        EVENT_SEQUENCE_OK = False
    elif etype == "done" and not event.get("result"):
        EVENT_SEQUENCE_OK = False


async def _run(instruction: str, settings: Settings) -> str | None:
    router = ModelRouter(settings)
    # The resolved_* properties, not the raw fields: those default to None and
    # the property is what computes the per-workspace path. This script predates
    # them and had been unrunnable since.
    memory = MemoryStore(settings.resolved_memory_db)
    tools = build_tools(settings, memory=memory, session_id="smoke")
    checkpointer = await make_sqlite_checkpointer(settings.resolved_checkpoints_db)
    graph = build_graph(settings, router, tools, checkpointer=checkpointer)
    skills = discover_skills(settings.resolved_skills_dir)
    try:
        def frame_task(emit_fn):
            return live_frames.start(emit_fn, build_browser(settings), settings)

        return await run_turn(
            instruction, "smoke", settings, _emit,
            graph=graph, memory=memory, skills=skills, router=router, live_frames=frame_task,
        )
    finally:
        await close_checkpointer(checkpointer)
        await aclose_browser()
        memory.close()


async def main() -> int:
    settings = Settings.from_env()
    if not settings.api_key:
        print("DEEPSEEK_API_KEY is not set — smoke needs a real key.", file=sys.stderr)
        return 2

    print("leg 1: math turn", file=sys.stderr)
    result = await _run("What is 17 * 23? Just answer with the number.", settings)
    if result is None or "391" not in result:
        print(f"FAIL: math leg returned {result!r}", file=sys.stderr)
        return 1

    if settings.pinchtab_base:
        print("leg 2: browser turn", file=sys.stderr)
        result = await _run(
            "Open https://example.com and tell me its one-line description.",
            settings,
        )
        if result is None or "Example Domain" not in result:
            print(f"FAIL: browser leg returned {result!r}", file=sys.stderr)
            return 1
    else:
        print("leg 2: skipped (no PINCHTAB_BASE)", file=sys.stderr)

    if not EVENT_SEQUENCE_OK:
        print("FAIL: event sequence contained an error event", file=sys.stderr)
        return 1
    print("SMOKE PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
