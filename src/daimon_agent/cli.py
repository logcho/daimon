"""Interactive/one-shot chat CLI. Events stream to stderr (pretty-printed),
the final result lands on stdout. One session (thread_id "cli") persists
across turns in interactive mode, with a live Jupyter kernel and real tools
behind it."""

from __future__ import annotations

import asyncio
import sys

from . import live_frames
from .browser import aclose_browser, build_browser
from .config import Settings
from .graph import build_graph, close_checkpointer, make_sqlite_checkpointer
from .memory import MemoryStore
from .model import ModelRouter
from .run import run_turn
from .tools import build_tools

SESSION_ID = "cli"


def _print_event(event: dict) -> None:
    etype = event.get("type")
    if etype == "step":
        status = event.get("status")
        label = event.get("label", "")
        if status == "running":
            print(f"  → {label}", file=sys.stderr, flush=True)
        elif status == "done":
            print(f"  ✓ {label}", file=sys.stderr, flush=True)
        elif status == "error":
            print(f"  ✗ {label}", file=sys.stderr, flush=True)
    elif etype == "error":
        print(f"error: {event.get('message')}", file=sys.stderr, flush=True)


async def _amain(argv: list[str]) -> int:
    settings = Settings.from_env()
    router = ModelRouter(settings)
    memory = MemoryStore(settings.memory_db)
    tools = build_tools(settings, memory=memory, session_id=SESSION_ID)
    checkpointer = await make_sqlite_checkpointer(settings.checkpoints_db)
    graph = build_graph(settings, router, tools, checkpointer=checkpointer)

    async def one_turn(instruction: str) -> int:
        def frame_task(emit_fn):
            return live_frames.start(emit_fn, build_browser(settings), settings)

        result = await run_turn(
            instruction, SESSION_ID, settings, _print_event,
            graph=graph, memory=memory, live_frames=frame_task,
        )
        if result is None:
            return 1
        print(result)
        return 0

    try:
        if argv:
            return await one_turn(" ".join(argv))

        if sys.stdin.isatty():
            print("Daimon chat (Ctrl-D to exit)", file=sys.stderr)
            while True:
                try:
                    line = input("you> ")
                except (EOFError, KeyboardInterrupt):
                    print(file=sys.stderr)
                    return 0
                if line.strip():
                    await one_turn(line.strip())
        else:
            return await one_turn(sys.stdin.read().strip())
    finally:
        # The aiosqlite worker thread is non-daemon; without this close the
        # process hangs at exit.
        await close_checkpointer(checkpointer)
        await aclose_browser()
        memory.close()


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
