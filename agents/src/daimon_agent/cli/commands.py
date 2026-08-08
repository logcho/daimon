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


def _make_commands() -> dict[str, tuple[str, CommandHandler]]:
    """Build the command table — returned as a function so the ``_RESET`` /
    ``_BLUE`` / ``_DIM`` / ``_BOLD`` constants can be passed in from the TUI
    or hard-coded here for shared access."""
    return {}


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
    lines.append(
        f"  {_DIM}Ctrl-D{_RESET}      {_DIM}exit{_RESET}"
    )
    lines.append(
        f"  {_DIM}Alt+Enter{_RESET}   {_DIM}submit (Enter = newline){_RESET}"
    )
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
    """``/model`` — show the current model configuration."""
    from ..config import Settings

    settings = Settings.from_env()
    lines: list[str] = []
    lines.append("")
    lines.append(f"{_DIM}── model{_RESET}")
    lines.append(f"  main       {_DIM}{settings.model}{_RESET}")
    flash = settings.resolved_flash_model
    if flash != settings.model:
        lines.append(f"  flash      {_DIM}{flash}{_RESET}")
    api = settings.api_base or "default"
    lines.append(f"  api_base   {_DIM}{api}{_RESET}")
    return lines


def _cmd_agents(text: str, session_name: str | None) -> list[str]:
    """``/agents`` — show available agents."""
    return [
        "",
        f"{_DIM}── agents{_RESET}",
        f"  {_BLUE}/general{_RESET}  {_DIM}browsing, research, notes, shell{_RESET}",
        f"  {_BLUE}/coding{_RESET}   {_DIM}kernel-first, file editing, shell, git{_RESET}",
        "",
        f"  {_DIM}type{_RESET} {_BLUE}/general{_RESET}"
        f" {_DIM}or{_RESET} {_BLUE}/coding{_RESET}"
        f" {_DIM}to switch, or press Shift+Tab{_RESET}",
    ]


# These are handled directly by the TUI (they mutate state.agent), but
# registered here so the completer and /help pick them up.
def _cmd_switch_agent(text: str, session_name: str | None) -> list[str]:
    """Switch agent — handled by the TUI directly."""
    return [""]  # never reached; the TUI intercepts before dispatch


def _cmd_tools(text: str, session_name: str | None) -> list[str]:
    """``/tools`` — handled by the TUI directly (needs HTTP client)."""
    return [""]  # never reached; the TUI intercepts before dispatch


# Register built-in commands
register("help", "show available commands", _cmd_help)
register("clear", "clear the output", _cmd_clear)
register("status", "session and terminal info", _cmd_status)
register("model", "current model configuration", _cmd_model)
register("agents", "list available agents", _cmd_agents)
register("tools", "list available tools for the current agent", _cmd_tools)
register("general", "switch to general agent", _cmd_switch_agent)
register("coding", "switch to coding agent", _cmd_switch_agent)
