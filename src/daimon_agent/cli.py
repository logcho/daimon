"""Interactive/one-shot chat CLI. Events stream to stderr (pretty-printed),
the final result lands on stdout. One session (thread_id "cli") persists
across turns in interactive mode."""

from __future__ import annotations

import asyncio
import sys

from .config import Settings
from .graph import build_graph, close_checkpointer, make_sqlite_checkpointer
from .model import ModelRouter
from .run import run_turn

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
    # Phase C wires the real tool registry here.
    checkpointer = await make_sqlite_checkpointer(settings.checkpoints_db)
    graph = build_graph(settings, router, [], checkpointer=checkpointer)

    async def one_turn(instruction: str) -> int:
        result = await run_turn(instruction, SESSION_ID, settings, _print_event, graph=graph)
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


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
