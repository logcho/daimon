"""Slash-command definitions for the daimon CLI.

Commands are handled locally — no agent round-trip. Each handler receives
the text after the slash and returns a list of output lines (raw ANSI strings
for the TUI) or None if the command was unrecognised.
"""

from __future__ import annotations

import shutil
from typing import Callable

# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------

CommandHandler = Callable[[str, str | None], list[str]]
"""A slash-command handler.

Args:
    text: the full raw text the user typed (including the leading ``/``).
    session_name: current session name (``None`` means "cli").

Returns:
    A list of raw-ANSI output lines to append to the TUI output area.
"""


# Registry built by ``register()`` called at import time.
_SLASH_COMMANDS: dict[str, tuple[str, CommandHandler]] = {}


def register(name: str, description: str, handler: CommandHandler) -> None:
    """Register a slash command (called at module import time)."""
    _SLASH_COMMANDS[name] = (description, handler)


def get_commands() -> dict[str, tuple[str, CommandHandler]]:
    """Return the command table: ``{name: (description, handler)}``."""
    return dict(_SLASH_COMMANDS)


# ---------------------------------------------------------------------------
# Built-in commands
# ---------------------------------------------------------------------------

# ANSI constants — same palette as the TUI (bypass isatty checks for embedding).
# Duplicated here rather than importing from ansi.py so the TUI can override
# them with Rich equivalents, but the defaults match the daimon palette.
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_MAGENTA = "\x1b[35m"
_BLUE = "\x1b[38;2;79;141;255m"  # #4f8dff
_RED = "\x1b[31m"


def _cmd_help(text: str, session_name: str | None) -> list[str]:
    """``/help`` — show available commands."""
    lines: list[str] = []
    lines.append("")
    lines.append(f"{_DIM}── commands{_RESET}")
    for name, (desc, _) in sorted(get_commands().items()):
        lines.append(
            f"  {_BLUE}/{name}{_RESET}"
            f"{' ' * (12 - len(name))}"
            f"{_DIM}{desc}{_RESET}"
        )
    lines.append("")
    for key, desc in (
        ("Enter", "submit"),
        ("Alt+Enter", "newline"),
        ("↑ ↓", "previous inputs"),
        ("Esc", "cancel the running turn"),
        ("Ctrl-C", "cancel, or exit when idle"),
        ("Ctrl-D", "exit"),
        ("wheel", "scroll the transcript"),
        ("PgUp PgDn", "scroll a page"),
        ("Shift+↑↓", "scroll a line"),
        ("Ctrl-End", "jump to the newest output"),
    ):
        lines.append(f"  {_DIM}{key}{_RESET}{' ' * max(12 - len(key), 1)}{_DIM}{desc}{_RESET}")
    return lines


def _cmd_clear(text: str, session_name: str | None) -> list[str]:
    """``/clear`` — reset the output area to the banner."""
    # Special return value convention: an empty list with a sentinel.
    # The TUI detects this and resets output_lines to the banner.
    # We use a special marker that the TUI checks.
    return ["__DAIMON_CLEAR__"]


def _cmd_status(text: str, session_name: str | None) -> list[str]:
    """``/status`` — show session and terminal info."""
    width = max(shutil.get_terminal_size((80, 20)).columns - 1, 1)
    height = shutil.get_terminal_size((80, 20)).lines
    name = session_name or "cli"
    lines: list[str] = []
    lines.append("")
    lines.append(f"{_DIM}── session{_RESET}")
    lines.append(f"  terminal   {width}×{height}")
    lines.append(f"  session    {_DIM}{name}{_RESET}")
    return lines


def _cmd_model(text: str, session_name: str | None) -> list[str]:
    """``/model`` — handled by the TUI directly (needs HTTP client)."""
    return [""]  # never reached; the TUI intercepts before dispatch


def _cmd_tools(text: str, session_name: str | None) -> list[str]:
    """``/tools`` — handled by the TUI directly (needs HTTP client)."""
    return [""]  # never reached; the TUI intercepts before dispatch


def _cmd_skills(text: str, session_name: str | None) -> list[str]:
    """``/skills [name]`` — handled by the TUI directly (needs HTTP client)."""
    return [""]  # never reached; the TUI intercepts before dispatch


def _cmd_plan(text: str, session_name: str | None) -> list[str]:
    """``/plan`` — handled by the TUI directly (it owns the session's mode)."""
    return [""]  # never reached; the TUI intercepts before dispatch


def _cmd_workspace(text: str, session_name: str | None) -> list[str]:
    """``/workspace [path]`` — show or change the agent's workspace directory.
    Without a path, shows the current workspace.  With a path, sets a new
    workspace (requires restarting the server on the next turn)."""
    import os
    from pathlib import Path

    from ..banner import workspace_full

    args = text.strip().split(maxsplit=1)
    if len(args) < 2:
        # Show current workspace
        ws = workspace_full()
        return [
            "",
            f"{_DIM}── workspace{_RESET}",
            f"  {ws}",
            "",
            f"  {_DIM}to change:{_RESET} {_BLUE}/workspace /path/to/dir{_RESET}",
        ]

    new_path = Path(args[1]).expanduser().resolve()
    if not new_path.is_dir():
        return [
            "",
            f"  {_RED}Error:{_RESET} not a directory: {args[1]}",
        ]

    os.environ["DAIMON_WORKSPACE_DIR"] = str(new_path)
    return [
        "",
        f"{_DIM}── workspace updated{_RESET}",
        f"  {new_path}",
        "",
        f"  {_DIM}The server will restart with the new workspace on the next turn.{_RESET}",
    ]


def _cmd_config(text: str, session_name: str | None) -> list[str]:
    """``/config`` — handled by the TUI directly (needs HTTP client)."""
    return [""]  # never reached; the TUI intercepts before dispatch


def _cmd_setup(text: str, session_name: str | None) -> list[str]:
    """``/setup`` — handled by the TUI directly (it runs the guided wizard)."""
    return [""]  # never reached; the TUI intercepts before dispatch


# Register built-in commands
register("help", "show available commands", _cmd_help)
register("clear", "clear the output", _cmd_clear)
register("status", "session and terminal info", _cmd_status)
register("model", "current model configuration", _cmd_model)
register("tools", "list available tools", _cmd_tools)
register("skills", "list/read skills · find <topic> · install <slug>", _cmd_skills)
register("plan", "toggle plan mode — confirm a plan before changes", _cmd_plan)
register("workspace", "show or change the workspace directory", _cmd_workspace)
register("config", "show the current configuration", _cmd_config)
register("setup", "set up a provider, key and models", _cmd_setup)
