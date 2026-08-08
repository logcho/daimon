"""Rich-based rendering for the daimon CLI.

Wraps `rich` primitives (Markdown, Panel, Text, Spinner, Syntax) in a
consistent daimon theme so every output path — one-shot, piped, TUI — shares
the same visual vocabulary.

All functions that return strings return plain text when the output is not a
TTY (Rich handles this automatically via ``Console``).
"""

from __future__ import annotations

import shutil
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.style import Style
from rich.text import Text
from rich.theme import Theme

# ---------------------------------------------------------------------------
# Daimon theme — blue accent #4f8dff
# ---------------------------------------------------------------------------

DAIMON_BLUE = "#4f8dff"
DAIMON_MAGENTA = "#d067d0"  # close to ANSI 35 (magenta) but tuned for readability
DAIMON_DIM = "#6b7280"       # gray-500
DAIMON_RED = "#ef4444"
DAIMON_GREEN = "#22c55e"
DAIMON_BG = "#0f0f1a"

_theme = Theme(
    {
        "markdown.code": "cyan",
        "markdown.code_block": "default",
        "markdown.heading": f"bold {DAIMON_BLUE}",
        "markdown.link": DAIMON_BLUE,
        "markdown.bold": "bold",
        "markdown.italic": "dim",
        "markdown.item.bullet": DAIMON_DIM,
        "markdown.item.number": DAIMON_DIM,
        "markdown.hr": DAIMON_DIM,
        "markdown.block_quote": DAIMON_DIM,
        "markdown.table.header": "bold",
        "repr.str": "cyan",
    }
)


def make_console(*, stderr: bool = False, plain: bool = False) -> Console:
    """Create a Rich ``Console`` with the daimon theme.

    Args:
        stderr: write to stderr (default stdout).
        plain: force plain-text output (for piped/non-TTY modes).
    """
    kwargs: dict[str, Any] = {"theme": _theme}
    if stderr:
        kwargs["stderr"] = True
    if plain:
        kwargs["force_terminal"] = False
    return Console(**kwargs)


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def render_markdown(text: str, *, console: Console | None = None) -> str:
    """Render markdown text with syntax-highlighted code blocks.

    Returns an ANSI string (or plain text when not a TTY).  For use inside a
    prompt_toolkit ``FormattedTextControl``, pass the result to ``ANSI()``.
    """
    c = console or make_console()
    with c.capture() as capture:
        c.print(Markdown(text, code_theme="monokai"))
    return capture.get().rstrip("\n")


def render_tool_step(
    name: str,
    status: str,
    *,
    label: str | None = None,
    elapsed: float | None = None,
    console: Console | None = None,
) -> str:
    """Render a single tool-call progress line.

    Args:
        name: tool name (e.g. ``"web_search"``).
        status: ``"running"``, ``"done"``, or ``"error"``.
        label: human-readable description (e.g. ``"searching the web"``).
        elapsed: seconds elapsed (shown for done/error).
    """
    c = console or make_console()

    if status == "running":
        mark = Text("→", style=Style(color=DAIMON_DIM))
    elif status == "done":
        mark = Text("✓", style=Style(color=DAIMON_DIM))
    elif status == "error":
        mark = Text("✗", style=Style(color=DAIMON_RED))
    else:
        return ""

    tool_name = Text(name, style=Style(color=DAIMON_MAGENTA))
    parts: list[Text] = [Text("  "), mark, Text(" "), tool_name]

    if label and label != name:
        parts.append(Text(" "))
        parts.append(Text(label, style=Style(color=DAIMON_DIM)))

    if elapsed is not None and status in ("done", "error"):
        parts.append(Text(" "))
        parts.append(Text(f"({elapsed:.1f}s)", style=Style(color=DAIMON_DIM)))

    with c.capture() as capture:
        c.print(Text.assemble(*parts))
    return capture.get().rstrip("\n")


def render_thinking(*, console: Console | None = None) -> str:
    """Render a thinking-indicator line (for non-TUI modes)."""
    c = console or make_console()
    spinner = Spinner("dots", text="Thinking…", style=Style(color=DAIMON_DIM))
    with c.capture() as capture:
        c.print(spinner)
    return capture.get().rstrip("\n")


def render_result(text: str, *, console: Console | None = None) -> str:
    """Render the agent's final result. Delegates to ``render_markdown``."""
    return render_markdown(text, console=console)


def render_status_line(
    session: str | None,
    model: str,
    turns: int,
    *,
    console: Console | None = None,
) -> str:
    """Render the bottom status bar.

    Returns a single line like ``cli · deepseek-chat · 12 turns``.
    """
    c = console or make_console()
    name = session or "cli"
    line = Text.assemble(
        (name, Style(color=DAIMON_DIM)),
        (" · ", Style(color=DAIMON_DIM)),
        (model, Style(color=DAIMON_DIM)),
        (" · ", Style(color=DAIMON_DIM)),
        (f"{turns} turn{'s' if turns != 1 else ''}", Style(color=DAIMON_DIM)),
    )
    with c.capture() as capture:
        c.print(line)
    return capture.get().rstrip("\n")


def render_divider(*, console: Console | None = None) -> str:
    """Render a dim horizontal divider line (terminal width)."""
    c = console or make_console()
    width = max(shutil.get_terminal_size((80, 20)).columns - 1, 1)
    line = Text("─" * width, style=Style(color=DAIMON_DIM))
    with c.capture() as capture:
        c.print(line)
    return capture.get().rstrip("\n")


def render_banner(
    session_name: str | None = None,
    *,
    console: Console | None = None,
) -> list[str]:
    """Return the daimon banner as a list of ANSI strings (delegates to
    ``banner.banner_lines()``).  Designed for embedding in the TUI output
    area — returns raw-ANSI strings compatible with prompt_toolkit's
    ``ANSI()`` parser."""
    from .. import banner

    return banner.banner_lines(session_name)


def print_banner(
    console: Console,
    *,
    session_name: str | None = None,
) -> None:
    """Print the banner directly to *console* (non-TUI mode). Delegates to
    ``banner.print_banner()``, writing to stderr."""
    import sys

    from .. import banner

    banner.print_banner(sys.stderr, session_name=session_name)


def to_ansi(renderable: Any, *, console: Console | None = None) -> str:
    """Convert any Rich renderable to an ANSI string.

    Useful for passing Rich output into prompt_toolkit's ``ANSI()`` parser.
    """
    c = console or make_console()
    with c.capture() as capture:
        c.print(renderable)
    return capture.get().rstrip("\n")
