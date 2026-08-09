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
    lines.append(
        f"  {_DIM}Enter{_RESET}      {_DIM}submit{_RESET}"
    )
    lines.append(
        f"  {_DIM}Alt+Enter{_RESET}   {_DIM}newline{_RESET}"
    )
    lines.append(
        f"  {_DIM}Ctrl-D{_RESET}      {_DIM}exit{_RESET}"
    )
    lines.append(
        f"  {_DIM}Shift+↑↓{_RESET}  {_DIM}scroll output{_RESET}"
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


def _cmd_tools(text: str, session_name: str | None) -> list[str]:
    """``/tools`` — handled by the TUI directly (needs HTTP client)."""
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


def _cmd_setup(text: str, session_name: str | None) -> list[str]:
    """``/setup`` — check configuration and show how to set up the agent."""
    import os
    from pathlib import Path

    from ..banner import workspace_full
    from ..config import Settings

    settings = Settings.from_env()

    # Detect where the key comes from
    env_file = Path.cwd() / ".env"
    has_env_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
    has_settings_key = bool(settings.api_key)
    key_ok = has_env_key or has_settings_key

    lines: list[str] = []
    lines.append("")
    lines.append(f"{_DIM}── setup{_RESET}")
    lines.append("")

    # API key
    if key_ok:
        src = (
            "environment variable"
            if has_env_key and not has_settings_key
            else ".env file" if has_settings_key and not has_env_key
            else ".env + environment"
        )
        lines.append(f"  {_DIM}api key{_RESET}    ✓ {_DIM}configured ({src}){_RESET}")
    else:
        lines.append(f"  {_DIM}api key{_RESET}    {_RED}✗ not set{_RESET}")
        lines.append(f"            {_DIM}add DEEPSEEK_API_KEY=sk-... to {env_file}{_RESET}")

    # Model
    lines.append(f"  {_DIM}model{_RESET}       {settings.model}")
    if settings.resolved_flash_model != settings.model:
        lines.append(f"  {_DIM}flash{_RESET}      {settings.resolved_flash_model}")

    # Workspace
    ws = workspace_full()
    lines.append(f"  {_DIM}workspace{_RESET}   {ws}")

    # API base
    api_base = settings.api_base or "(default)"
    lines.append(f"  {_DIM}api base{_RESET}   {api_base}")

    # PinchTab
    pinch_ok = settings.pinchtab_token is not None
    if pinch_ok:
        lines.append(f"  {_DIM}pinchtab{_RESET}    ✓ {settings.pinchtab_base}")
    else:
        lines.append(f"  {_DIM}pinchtab{_RESET}    {_DIM}not configured{_RESET}")

    lines.append("")
    lines.append(f"  {_DIM}config file{_RESET} {env_file}")
    if not key_ok:
        lines.append("")
        lines.append(f"  {_BOLD}To get started:{_RESET}")
        lines.append(f"  1. Get an API key from {_BLUE}platform.deepseek.com{_RESET}")
        lines.append(f"  2. Add it to {_DIM}{env_file}{_RESET}:")
        lines.append(f"     {_DIM}DEEPSEEK_API_KEY=sk-...{_RESET}")
        lines.append(f"  3. Restart the daimon server or run {_BLUE}daimon --stop{_RESET} first")
    lines.append("")
    if key_ok:
        lines.append(f"  {_DIM}Ready. Type a question to start.{_RESET}")
    return lines


# Register built-in commands
register("help", "show available commands", _cmd_help)
register("clear", "clear the output", _cmd_clear)
register("status", "session and terminal info", _cmd_status)
register("model", "current model configuration", _cmd_model)
register("tools", "list available tools", _cmd_tools)
register("workspace", "show or change the workspace directory", _cmd_workspace)
register("setup", "check configuration and setup guide", _cmd_setup)
