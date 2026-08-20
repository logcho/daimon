"""Entry point for the daimon CLI — thin client of the background server.

``main.py`` owns arg parsing, mode dispatch, and the non-TUI turn loop.
The heavy stack (graph, tools, browser, memory) loads once in the server;
the CLI imports stdlib + aiohttp + rich + (lazily) prompt_toolkit.

One session (``thread_id="cli"``) persists across turns by default;
``-n work`` starts a second concurrent conversation.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import sys
from pathlib import Path

import aiohttp

from .. import client
from ..ansi import cyan, dim, erase_line, red
from ..config import Settings
from . import display

SESSION_ID = "cli"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="daimon",
        description="Chat with the daimon agent. A background server keeps every "
        "session alive between invocations; `daimon --stop` shuts it down.",
    )
    parser.add_argument(
        "-n",
        "--name",
        default=SESSION_ID,
        help="session name (default: %(default)s — one continuous conversation; "
        "a different name starts a second, concurrent one)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list existing session names and exit",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="stop the background server the CLI spawned and exit",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show version and exit",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="run diagnostics on the daimon installation and exit",
    )
    parser.add_argument(
        "instruction",
        nargs="*",
        help="ask once and exit (skips the interactive prompt when non-empty)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Non-TUI streaming display
# ---------------------------------------------------------------------------


class _StreamDisplay:
    """Progress for the non-interactive paths — one-shot and piped stdin.

    Steps are printed to stderr as they *finish*, one line each, so the result
    on stdout stays clean and pipeable. There is no animation and no rewriting:
    this path may be writing to a file or a pipe, where escape codes are noise.
    The interactive TUI is the one that animates.
    """

    def __init__(self, *, tty: bool) -> None:
        self._tty = tty
        self._marks = {"done": "✓", "error": "✗"}

    def on_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "step":
            status = event.get("status")
            name = event.get("tool") or event.get("label", "")
            if name == "Thinking" or status not in self._marks:
                return
            mark = self._marks[status]
            styled = red(mark, sys.stderr) if status == "error" else dim(mark, sys.stderr)
            line = f"  {styled} {name}"
            detail = event.get("detail")
            if detail:
                line += f" {dim(str(detail), sys.stderr)}"
            print(erase_line(sys.stderr) + line, file=sys.stderr, flush=True)

        elif etype == "error":
            print(
                erase_line(sys.stderr)
                + f"  {red('✖ Error:', sys.stderr)} {event.get('message', '')}",
                file=sys.stderr,
                flush=True,
            )

    def finish(self, *, elapsed: float | None = None) -> None:
        """Called when the turn completes (done/error event received)."""
        return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)

    if args.version:
        from .. import __version__

        print(f"daimon {__version__}")
        return 0

    # ---- workspace: default to the directory the CLI was opened in ---------
    # Set before every branch below — --stop/--doctor/--list must resolve
    # the same per-workspace run dir a normal `daimon` invocation would use,
    # or they'd silently operate on the wrong workspace's server.
    if "DAIMON_WORKSPACE_DIR" not in os.environ:
        os.environ["DAIMON_WORKSPACE_DIR"] = os.getcwd()

    if args.doctor:
        from .doctor import format_report, run_doctor

        report = await run_doctor()
        print(format_report(report))
        return 0

    if args.stop:
        ok, message = await client.stop_server(Settings.from_env())
        print(message)
        return 0 if ok else 1

    settings = Settings.from_env()

    if args.list:
        for name in client.list_sessions(settings.resolved_checkpoints_db):
            print(name)
        return 0

    interactive = sys.stdin.isatty() and not args.instruction

    # ---- workspace permission prompt (interactive only) --------------------
    if interactive:
        ws_path = Path(os.environ["DAIMON_WORKSPACE_DIR"]).resolve()
        # Show a confirmation line so the user knows where the agent operates.
        print(
            f"\n  Workspace: {ws_path}\n"
            f"  Press Enter to confirm, or type a different path: ",
            file=sys.stderr,
            end="",
            flush=True,
        )
        try:
            alt = input()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 0
        if alt.strip():
            new_path = Path(alt.strip()).expanduser().resolve()
            if new_path.is_dir():
                os.environ["DAIMON_WORKSPACE_DIR"] = str(new_path)
                ws_path = new_path
                # Re-resolve: `settings` was already built above, before this
                # prompt could move the workspace — without this, the
                # confirmation dialog's typed override would have no effect
                # and ensure_server would silently use the original cwd.
                settings = Settings.from_env()
                print(f"  Workspace changed to: {ws_path}\n", file=sys.stderr)
            else:
                print(
                    f"  {red('Error:', sys.stderr)} not a directory: {alt.strip()}\n",
                    file=sys.stderr,
                )
                return 1
        else:
            print(file=sys.stderr)  # blank line after confirmation

    timeout = aiohttp.ClientTimeout(total=None)

    async with aiohttp.ClientSession(timeout=timeout) as http:

        async def one_turn(instruction: str) -> int:
            port, _spawned = await client.ensure_server(settings)

            tty_stderr = sys.stderr.isatty()
            stream = _StreamDisplay(tty=tty_stderr)

            def emit(event: dict) -> None:
                stream.on_event(event)

            try:
                # No "ask" capability advertised: a one-shot invocation has
                # nobody to answer a question, so the server withholds the
                # ask tools and every turn ends in done|error.
                terminal = await client.stream_turn(
                    http, port, args.name, instruction, emit
                )
                result = client.turn_result(terminal)
                if result is None:
                    return 1
                # Print result to stdout
                rendered = display.render_result(result)
                print(rendered)
                return 0
            finally:
                stream.finish()

        if args.instruction:
            return await one_turn(" ".join(args.instruction))

        if interactive:
            session_name = args.name if args.name != SESSION_ID else None

            if sys.stderr.isatty():
                # --- Full-screen TUI ---
                # prompt_toolkit imported lazily — ~120ms, keeps one-shot fast
                from .tui import run_tui

                return await run_tui(settings, session_name, http)
            else:
                # --- Legacy fallback (stderr redirected) ---
                # Print simplified banner to stderr
                console = display.make_console(stderr=True)
                display.print_banner(console, session_name=session_name)

                while True:
                    try:
                        line = input(cyan("> ", sys.stdout))
                    except (EOFError, KeyboardInterrupt):
                        print(file=sys.stderr)
                        return 0
                    if line.strip():
                        exit_code = await one_turn(line.strip())
                        # Divider between turns
                        print(
                            dim(
                                "─"
                                * max(
                                    shutil.get_terminal_size((80, 20)).columns
                                    - 1,
                                    1,
                                ),
                                sys.stderr,
                            ),
                            file=sys.stderr,
                        )
        else:
            return await one_turn(sys.stdin.read().strip())


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
