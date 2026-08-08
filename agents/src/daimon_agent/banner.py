"""Banner for the Daimon CLI — simplified wordmark + CWD layout.

The old 15-line block-character ASCII art is preserved as ``BANNER_LINES``
(for tests and anyone who wants the full art), but the default rendering
uses a clean, Claude Code-style wordmark-only banner.

All styling is deferred to callers via ``ansi.py`` helpers so this module
is pure data — no escape codes, no tty checks.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Full ASCII art (preserved, not rendered by default)
# ---------------------------------------------------------------------------

BANNER_LINES = [
    ("dim", "         █    ██                              "),
    ("dim", "       ███  ███   █                           "),
    ("dim", "      ██████████████████                      "),
    ("dim", "     ██████████████████████                   "),
    ("dim", "    ████▓▓█████████████████████               "),
    ("dim", "    ████████████████████████████              "),
    ("dim", "    █████████████████████████████             "),
    ("dim", "     ██   ████████████████████████            "),
    ("dim", "          █████████████████████████           "),
    ("dim", "           ████████████████████████           "),
    ("dim", "           █████████████████████████          "),
    ("dim", "           ████████████████████████ █         "),
    ("dim", "           █████████████████████ ██           "),
    ("dim", "        █████████████████████████  █          "),
    ("dim", "        ███████████████████████               "),
]

BANNER = "\n".join(text for _, text in BANNER_LINES)


# ---------------------------------------------------------------------------
# Simplified banner (default)
# ---------------------------------------------------------------------------

def _shorten_path(path: str) -> str:
    """Shorten a path for display: ~ substitution + truncation."""
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) > 50:
        path = "…" + path[-48:]
    return path


def workspace_display() -> str:
    """Resolved workspace directory (absolute, shortened for display)."""
    ws = os.environ.get("DAIMON_WORKSPACE_DIR") or os.getcwd()
    return _shorten_path(os.path.abspath(ws))


def workspace_full() -> str:
    """Resolved workspace directory (absolute, full path)."""
    ws = os.environ.get("DAIMON_WORKSPACE_DIR") or os.getcwd()
    return os.path.abspath(ws)


def print_banner(stream: object, *, session_name: str | None = None) -> None:
    """Print the simplified banner to *stream* (usually stderr).

    Three lines + blank::

        daimon
        Workspace: ~/projects/my-app
        Enter submit  ·  Alt+Enter newline  ·  Ctrl-D exit  [session]  ·  /help
    """
    from .ansi import blue, bold, dim

    ws = workspace_display()
    print(blue(bold("daimon", stream), stream), file=stream)
    print(f"  {dim('Workspace:', stream)} {ws}", file=stream)

    hint = "Enter submit  ·  Alt+Enter newline  ·  Ctrl-D exit"
    if session_name:
        hint += f"  [{session_name}]"
    hint += "  ·  /help  ·  /workspace"
    print(dim(hint, stream), file=stream)
    print(file=stream)


def banner_lines(session_name: str | None = None) -> list[str]:
    """Return the simplified banner as raw-ANSI strings for embedding in the
    TUI output area (parsed by prompt_toolkit's ``ANSI()``).

    Uses ansi.py's module-level constants directly — no per-stream isatty()
    check, so styling is always applied inside the TUI.
    """
    from .ansi import BLUE, BOLD, DIM, RESET

    ws = workspace_display()

    line0 = f"{BLUE}{BOLD}daimon{RESET}"
    line1 = f"  {DIM}Workspace:{RESET} {ws}"

    hint = "Enter submit  ·  Alt+Enter newline  ·  Ctrl-D exit"
    if session_name:
        hint += f"  [{session_name}]"
    hint += "  ·  /help  ·  /workspace"
    line2 = f"{DIM}{hint}{RESET}"

    return [line0, line1, line2, ""]


# ---------------------------------------------------------------------------
# Full ASCII art (opt-in)
# ---------------------------------------------------------------------------

def full_banner_lines(session_name: str | None = None) -> list[str]:
    """Return the full 15-line block-character ASCII art banner as raw-ANSI
    strings.  Opt-in — the default ``banner_lines()`` is the simplified
    wordmark version.
    """
    from .ansi import BLUE, BOLD, DIM, RESET

    max_art_width = max(len(text) for _, text in BANNER_LINES)
    ws = workspace_display()

    lines: list[str] = []
    for i, (_style, text) in enumerate(BANNER_LINES):
        art_chars: list[str] = []
        for ch in text:
            if ch == "▓":
                art_chars.append(f"{BLUE}{ch}{RESET}")
            elif ch == "█":
                art_chars.append(f"{DIM}{ch}{RESET}")
            else:
                art_chars.append(ch)
        art = "".join(art_chars)
        art = art + " " * (max_art_width - len(text) + 2)

        if i == 0:
            info = f"{BLUE}{BOLD}daimon{RESET}"
        elif i == 1:
            info = f"  {DIM}Workspace:{RESET} {ws}"
        elif i == 2:
            hint = "Enter submit  ·  Alt+Enter newline  ·  Ctrl-D exit"
            if session_name:
                hint += f"  [{session_name}]"
            hint += "  ·  /help  ·  /workspace"
            info = f"{DIM}{hint}{RESET}"
        else:
            info = ""
        lines.append(f"{art}{info}" if info else art)

    lines.append("")
    return lines
