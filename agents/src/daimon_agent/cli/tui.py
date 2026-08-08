"""prompt_toolkit full-screen TUI for the daimon CLI.

Claude Code-style layout: multiline ``> `` prompt, Rich-rendered output area,
thinking animation with cycling verbs, inline tool-call display, persistent
status line, and a completion menu for slash commands.

Imported lazily by ``main.py`` so the one-shot path stays fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from typing import Any

from prompt_toolkit.application import Application, create_app_session
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output import create_output
from prompt_toolkit.styles import Style

from .. import client
from ..config import Settings
from . import commands as cmd_mod
from . import display

# ---------------------------------------------------------------------------
# Raw ANSI constants — always styled inside the TUI
# ---------------------------------------------------------------------------

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_RED = "\x1b[31m"
_MAGENTA = "\x1b[35m"
_BLUE = "\x1b[38;2;79;141;255m"  # #4f8dff

# ---------------------------------------------------------------------------
# Spinner / thinking animation
# ---------------------------------------------------------------------------

# Claude Code spinner frames
_SPINNER_FRAMES = ["·", "✢", "*", "✶", "✻", "✽"]
_SPINNER_INTERVAL = 0.1  # seconds per frame

# Cycling verbs for the thinking panel
_THINKING_VERBS = [
    "Thinking…",
    "Ruminating…",
    "Discombobulating…",
    "Consulting the oracles…",
    "Exploring…",
    "Chasing will-o'-wisps…",
    "Fiddling with knobs…",
    "Flibbertigibbeting…",
]
_VERB_INTERVAL = 2.0  # seconds per verb

# Completion markers (swapped in when thinking finishes without steps)
_COMPLETION_VERBS = ["Baked", "Conjured", "Served", "Summoned"]

# Thinking-panel border shimmer colours (cycling every ~500ms)
_SHIMMER_COLORS = ["#4f8dff", "#3b6fcc", "#2d5bb5"]


def _thinking_panel(frame: str, verb: str, shimmer_idx: int, width: int) -> list[str]:
    """Build a 3-line open-left thinking panel.

    Returns three ANSI strings::
        ┌────────────────────────────────────────────
        │ {frame} {verb}
        └────────────────────────────────────────────
    """
    border_color = _SHIMMER_COLORS[shimmer_idx % len(_SHIMMER_COLORS)]
    inner = f"{shimmer_color(border_color)}│{_RESET} {_DIM}{frame}{_RESET} {_DIM}{verb}{_RESET}"
    w = max(width - 1, 40)
    top = f"{shimmer_color(border_color)}┌{'─' * w}{_RESET}"
    bot = f"{shimmer_color(border_color)}└{'─' * w}{_RESET}"
    return [top, inner, bot]


def _thinking_done(verb: str, elapsed: float) -> str:
    """Collapsed completion line replacing the thinking panel."""
    return f"  {_DIM}✓{_RESET} {_DIM}{verb}{_RESET} {_DIM}({elapsed:.1f}s){_RESET}"


def shimmer_color(hex_color: str) -> str:
    """Convert a hex colour to a 24-bit ANSI escape."""
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


# ---------------------------------------------------------------------------
# TUI state
# ---------------------------------------------------------------------------

class TuiState:
    """Mutable state for the TUI, shared between the app and the ticker."""

    def __init__(self, http: Any) -> None:
        self.output_lines: list[str] = []
        self.turn_count: int = 0
        self.last_elapsed: float | None = None
        self.http: Any = http  # aiohttp.ClientSession for /tools and other server calls
        self._frame_idx: int = 0
        self._verb_idx: int = 0
        self._verb_elapsed: float = 0.0
        self._thinking_start: float | None = None
        self._thinking_lines: tuple[int, int] | None = None  # (start, end) in output_lines
        self._running_steps: dict[str, int] = {}  # step_id → line_index
        self._redraw: Any = lambda: None
        self._auto_scroll: bool = True  # tracks whether we should scroll to bottom


def _render_output(state: TuiState) -> ANSI:
    return ANSI("\n".join(state.output_lines))


def _cursor_at_end(state: TuiState) -> Point:
    return Point(x=0, y=max(0, len(state.output_lines) - 1))


# ---------------------------------------------------------------------------
# Slash completer
# ---------------------------------------------------------------------------

class _SlashCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        word = text[1:]
        for cmd, (desc, _) in cmd_mod.get_commands().items():
            if cmd.startswith(word):
                yield Completion(
                    cmd,
                    start_position=-len(word),
                    display=f"/{cmd}",
                    display_meta=desc,
                )


# ---------------------------------------------------------------------------
# TUI application
# ---------------------------------------------------------------------------


async def run_tui(
    settings: Settings,
    session_name: str | None,
    http: Any,  # aiohttp.ClientSession
) -> int:
    """Launch the full-screen prompt_toolkit TUI.  Returns 0 on clean exit.

    The heavy import is here so ``--help`` / ``--list`` / one-shot paths
    never pay the ~120ms prompt_toolkit cost.
    """
    state = TuiState(http)
    state.output_lines = display.render_banner(session_name)

    # --- Layout ----------------------------------------------------------

    width, height = shutil.get_terminal_size((80, 20))

    output_window = Window(
        content=FormattedTextControl(
            text=lambda: _render_output(state),
            focusable=False,
            # When auto-scrolling, put the cursor at the bottom so
            # prompt_toolkit's built-in _scroll() follows new output.
            # When the user scrolls up, put the cursor at the first
            # visible line — do_scroll() inside _scroll() then sees the
            # cursor is already visible and leaves vertical_scroll alone.
            get_cursor_position=lambda: _cursor_at_end(state)
            if state._auto_scroll
            else Point(0, output_window.vertical_scroll),
        ),
        wrap_lines=True,
        always_hide_cursor=True,
        right_margins=[ScrollbarMargin(display_arrows=True)],
    )

    # Monkey-patch the Window's mouse handler so mouse-wheel scrolling
    # sets _auto_scroll = False (the built-in handler changes vertical_scroll
    # but doesn't know about our auto-scroll flag, so cursor-following would
    # immediately undo the scroll on the next render).
    _orig_mouse_handler = output_window._mouse_handler

    def _mouse_handler(mouse_event):
        if mouse_event.event_type in (
            MouseEventType.SCROLL_UP,
            MouseEventType.SCROLL_DOWN,
        ):
            state._auto_scroll = False
        return _orig_mouse_handler(mouse_event)

    output_window._mouse_handler = _mouse_handler.__get__(output_window)

    input_buffer = Buffer(
        multiline=True,
        completer=_SlashCompleter(),
        complete_while_typing=True,
    )
    input_window = Window(
        content=BufferControl(
            buffer=input_buffer,
            input_processors=[BeforeInput(ANSI(f"{_BLUE}> {_RESET}"))],
        ),
        height=Dimension(min=1),
        wrap_lines=True,
        dont_extend_height=True,
    )

    divider = Window(height=1, char="─", style="class:divider")

    status_window = Window(
        content=FormattedTextControl(
            text=lambda: _render_status(state, session_name, settings),
            focusable=False,
        ),
        height=1,
        dont_extend_height=True,
    )

    root = HSplit([output_window, divider, input_window, status_window])
    body = FloatContainer(
        content=root,
        floats=[
            Float(
                xcursor=True,
                ycursor=True,
                transparent=True,
                content=CompletionsMenu(
                    max_height=12, scroll_offset=1, display_arrows=True
                ),
            ),
        ],
    )
    layout = Layout(body, focused_element=input_window)

    # --- Redraw helper ---------------------------------------------------

    def _redraw() -> None:
        state._redraw()

    # --- Scroll helpers --------------------------------------------------

    def _scroll_to_bottom() -> None:
        """Force the output window to show the bottom of the content."""
        state._auto_scroll = True
        ri = output_window.render_info
        if ri:
            total = len(state.output_lines)
            output_window.vertical_scroll = max(0, total - ri.window_height)
        else:
            output_window.vertical_scroll = 1_000_000

    def _page_up() -> None:
        """Scroll output up by half a page."""
        state._auto_scroll = False
        ri = output_window.render_info
        page = max((ri.window_height if ri else 20) // 2, 1)
        output_window.vertical_scroll = max(0, output_window.vertical_scroll - page)

    def _page_down() -> None:
        """Scroll output down by half a page, or snap to bottom."""
        ri = output_window.render_info
        page = max((ri.window_height if ri else 20) // 2, 1)
        output_window.vertical_scroll += page
        # If we're past the end, snap to auto-scroll (bottom).
        total = len(state.output_lines)
        if ri and output_window.vertical_scroll + ri.window_height >= total:
            _scroll_to_bottom()

    # Redraw snaps to bottom only when auto-scroll is on (default).  When the
    # user pages up, auto-scroll disables and the view stays where they left
    # it.  New-turn start and user-initiated scroll-to-bottom restore it.
    def _redraw_snap() -> None:
        if state._auto_scroll:
            _scroll_to_bottom()
        app.invalidate()

    state._redraw = _redraw_snap

    # --- Key bindings ----------------------------------------------------

    kb = KeyBindings()

    @kb.add("c-d")
    @kb.add("c-c")
    def _exit_app(event):
        event.app.exit()

    @kb.add("tab")
    def _tab(event):
        buf = event.app.current_buffer
        if buf.complete_state:
            buf.complete_next()
        else:
            buf.start_completion(select_first=True)

    @kb.add(
        "up", filter=Condition(lambda: input_buffer.complete_state is not None)
    )
    def _up(event):
        input_buffer.complete_previous()

    @kb.add(
        "down", filter=Condition(lambda: input_buffer.complete_state is not None)
    )
    def _down(event):
        input_buffer.complete_next()

    @kb.add("enter")
    def _submit(event):
        buf = event.app.current_buffer
        # Apply highlighted completion first
        if buf.complete_state and buf.complete_state.current_completion:
            buf.apply_completion(buf.complete_state.current_completion)

        text = buf.text.strip()
        if not text:
            return
        buf.reset()
        if text.startswith("/"):
            _handle_slash(text, state, event.app)
        else:
            event.app.create_background_task(_run_turn(text, state, settings, session_name))

    @kb.add("escape", "enter")
    def _newline(event):
        event.app.current_buffer.insert_text("\n")

    @kb.add("s-pageup")
    def _page_up_kb(event):
        """Shift+PageUp: scroll output up by half a page."""
        _page_up()

    @kb.add("s-pagedown")
    def _page_down_kb(event):
        """Shift+PageDown: scroll output down by half a page."""
        _page_down()

    @kb.add("c-up")
    def _scroll_line_up(event):
        """Ctrl-Up: scroll output up by one line."""
        state._auto_scroll = False
        output_window.vertical_scroll = max(0, output_window.vertical_scroll - 1)

    @kb.add("c-down")
    def _scroll_line_down(event):
        """Ctrl-Down: scroll output down by one line."""
        ri = output_window.render_info
        total = len(state.output_lines)
        output_window.vertical_scroll += 1
        if ri and output_window.vertical_scroll + ri.window_height >= total:
            _scroll_to_bottom()

    # --- Tools listing (async — fetches from server) ----------------------

    async def _show_tools(state: TuiState, session_name: str | None) -> None:
        """Fetch the tool list from the server and display it grouped by
        category (file ops, execution, search, etc.)."""
        port, _ = await client.ensure_server(settings)
        tools = await client.list_tools(state.http, port)
        if not tools:
            state.output_lines.append("")
            state.output_lines.append(
                f"  {_DIM}no tools found — is the agent server running?{_RESET}"
            )
            _redraw()
            return

        # Group tools by category
        groups: dict[str, list[str]] = {}
        for t in tools:
            name = t["name"]
            desc = t.get("description", "")
            # Categorize by name prefix
            if name in ("read_file", "write_file", "edit_file", "mkdir",
                        "list_directory", "delete_file", "move_file",
                        "glob_files", "grep_files"):
                cat = "file ops"
            elif name in ("kernel_execute", "run_shell"):
                cat = "execution"
            elif name in ("check_code", "debug", "run_tests"):
                cat = "code quality"
            elif name in ("web_search", "web_fetch"):
                cat = "search"
            elif name in ("recall", "list_skills", "read_skill", "save_skill"):
                cat = "memory & skills"
            elif name in ("open_url", "read_page", "click", "fill_field",
                          "new_tab", "switch_tab", "close_tab", "extract_text"):
                cat = "browser"
            elif name == "stage_terminal_command":
                cat = "host"
            elif name == "research":
                cat = "research"
            else:
                cat = "other"
            groups.setdefault(cat, []).append(f"  {_BLUE}{name}{_RESET}  {_DIM}{desc}{_RESET}")

        state.output_lines.append("")
        state.output_lines.append(
            f"{_DIM}── tools{_RESET}"
        )
        order = ["file ops", "execution", "code quality", "search",
                 "browser", "memory & skills", "host", "research", "other"]
        for cat in order:
            if cat in groups:
                state.output_lines.append(f"  {_DIM}{cat}:{_RESET}")
                state.output_lines.extend(groups[cat])
        state.output_lines.append(
            f"  {_DIM}{len(tools)} tool(s) total{_RESET}"
        )
        _redraw()

    # --- Slash command handler -------------------------------------------

    def _handle_slash(text: str, state: TuiState, app: Any) -> None:
        cmd_name = text[1:].strip().lower()

        # Aliases
        aliases = {
            "h": "help",
            "?": "help",
            "cls": "clear",
            "st": "status",
            "exit": "exit",
            "quit": "exit",
            "q": "exit",
            "ws": "workspace",
        }
        cmd_name = aliases.get(cmd_name, cmd_name)

        if cmd_name == "exit":
            app.exit()
            return

        # /tools — fetches the tool list from the server for the current agent
        if cmd_name == "tools":
            app.create_background_task(_show_tools(state, session_name))
            return

        all_cmds = cmd_mod.get_commands()
        if cmd_name in all_cmds:
            _, handler = all_cmds[cmd_name]
            lines = handler(text, session_name)
            if lines and lines == ["__DAIMON_CLEAR__"]:
                state.output_lines.clear()
                state.output_lines.extend(display.render_banner(session_name))
            else:
                state.output_lines.extend(lines)
        else:
            state.output_lines.append("")
            state.output_lines.append(f"  {_DIM}unknown:{_RESET} {text}")
            state.output_lines.append(
                f"  {_DIM}type{_RESET} {_BLUE}/help{_RESET}"
                f" {_DIM}for commands{_RESET}"
            )
        _redraw()

    # --- Agent turn (background task) ------------------------------------

    def _on_tui_event(event: dict) -> None:
        """Step/error events → output_lines with raw ANSI.

        Tool steps appear inline beneath the thinking panel during the turn so
        the user sees the agent's plan through its actions.  The thinking panel
        stays visible through tool execution and collapses when the agent
        finishes.
        """
        etype = event.get("type")
        if etype == "step":
            status = event.get("status")
            label = event.get("label", "")
            tool = event.get("tool")
            step_id = event.get("id", "")
            name = tool or label

            if name == "Thinking":
                if status == "running":
                    # Start thinking panel
                    state._thinking_start = time.monotonic()
                    width = max(
                        shutil.get_terminal_size((80, 20)).columns - 1, 1
                    )
                    panel = _thinking_panel(
                        _SPINNER_FRAMES[0],
                        _THINKING_VERBS[0],
                        0,
                        width,
                    )
                    start_idx = len(state.output_lines)
                    state.output_lines.extend(panel)
                    state._thinking_lines = (start_idx, len(state.output_lines))
                elif status == "done":
                    if state._thinking_lines is not None:
                        elapsed = (
                            time.monotonic() - state._thinking_start
                            if state._thinking_start
                            else 0.0
                        )
                        verb = _COMPLETION_VERBS[
                            state._turn_count_for_verb
                            % len(_COMPLETION_VERBS)
                        ]
                        s, e = state._thinking_lines
                        state.output_lines[s:e] = [
                            _thinking_done(verb, elapsed)
                        ]
                        state._thinking_lines = None
                        state._thinking_start = None
                        # Shift running step indices
                        shift = (e - s) - 1
                        if shift != 0:
                            for sid in list(state._running_steps):
                                if state._running_steps[sid] > e:
                                    state._running_steps[sid] -= shift
                return

            # Tool step — show inline beneath the thinking panel so the user
            # sees the agent's plan unfold through its actions.
            if status == "running":
                mark = f"{_DIM}→{_RESET}"
            elif status == "done":
                mark = f"{_DIM}✓{_RESET}"
            elif status == "error":
                mark = f"{_RED}✗{_RESET}"
            else:
                return

            line = f"  {mark} {_MAGENTA}{name}{_RESET}"
            if tool and label and label != tool:
                line += f" {_DIM}{label}{_RESET}"

            if status == "running":
                idx = len(state.output_lines)
                state.output_lines.append(line)
                state._running_steps[step_id] = idx
            elif step_id in state._running_steps:
                idx = state._running_steps.pop(step_id)
                state.output_lines[idx] = line
            else:
                state.output_lines.append(line)

        elif etype == "error":
            # Drop thinking panel if present
            if state._thinking_lines is not None:
                s, e = state._thinking_lines
                del state.output_lines[s:e]
                state._thinking_lines = None
                state._thinking_start = None
            state.output_lines.append(
                f"  {_RED}✖ Error:{_RESET} {event.get('message', '')}"
            )
            _redraw()

    async def _run_turn(
        text: str,
        state: TuiState,
        settings: Settings,
        session_name: str | None,
    ) -> None:
        # New turn — always restore auto-scroll so the user sees the response.
        state._auto_scroll = True

        # User message line
        state.output_lines.append("")
        state.output_lines.append(f"{_BLUE}>{_RESET} {_BOLD}{text}{_RESET}")

        # Save verb index for completion marker
        state._turn_count_for_verb = state.turn_count % len(_COMPLETION_VERBS)

        _redraw()

        # Track elapsed time
        t0 = time.monotonic()

        try:
            port, _ = await client.ensure_server(settings)
            result = await client.stream_turn(
                http,
                port,
                session_name or "cli",
                text,
                _on_tui_event,
            )

            # Drop thinking panel if still present (no tool steps fired)
            if state._thinking_lines is not None:
                s, e = state._thinking_lines
                del state.output_lines[s:e]
                state._thinking_lines = None
                state._thinking_start = None

            elapsed = time.monotonic() - t0
            state.last_elapsed = elapsed
            state.turn_count += 1

            if result:
                # Render markdown result
                width = max(shutil.get_terminal_size((80, 20)).columns - 1, 1)
                rendered = display.render_markdown(result)
                state.output_lines.append(rendered)

            state.output_lines.append(f"{_DIM}──{_RESET}")
        except client.ClientError as exc:
            state.output_lines.append(f"{_RED}Error: {exc}{_RESET}")
        _redraw()

    # --- Animation ticker ------------------------------------------------

    async def _ticker(app: Application) -> None:
        """Background task: animate spinner frames + cycling verbs."""
        while True:
            await asyncio.sleep(_SPINNER_INTERVAL)

            state._frame_idx += 1
            state._verb_elapsed += _SPINNER_INTERVAL

            if state._verb_elapsed >= _VERB_INTERVAL:
                state._verb_elapsed = 0.0
                state._verb_idx = (state._verb_idx + 1) % len(_THINKING_VERBS)

            # Update thinking panel if visible
            if state._thinking_lines is not None:
                s, e = state._thinking_lines
                frame = _SPINNER_FRAMES[
                    state._frame_idx % len(_SPINNER_FRAMES)
                ]
                verb = _THINKING_VERBS[
                    state._verb_idx % len(_THINKING_VERBS)
                ]
                shimmer = state._frame_idx // 5  # ~500ms per shimmer step
                width = max(
                    shutil.get_terminal_size((80, 20)).columns - 1, 1
                )
                panel = _thinking_panel(frame, verb, shimmer, width)
                state.output_lines[s:e] = panel
                app.invalidate()

            # Tool steps are hidden behind the thinking panel — no inline
            # spinner animation needed.  The thinking panel already animates.

    # --- Status line -----------------------------------------------------

    def _render_status(
        state: TuiState,
        session_name: str | None,
        settings: Settings,
    ) -> ANSI:
        name = session_name or "cli"
        model = settings.model
        parts = [f"{_DIM}{name} · {model}"]

        if state.turn_count > 0:
            ts = "turn" if state.turn_count == 1 else "turns"
            parts.append(f"{state.turn_count} {ts}")

        if state.last_elapsed is not None:
            parts.append(f"{state.last_elapsed:.1f}s")

        return ANSI(f"{_DIM}{' · '.join(parts)}{_RESET}")

    # --- Launch ----------------------------------------------------------

    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        style=Style.from_dict(
            {
                "divider": "#374151",
                "completion-menu": "bg:#1a1a2e #e5e5e5",
                "completion-menu.completion.current": "bg:#4f8dff #ffffff",
                "completion-menu.meta.completion": "bg:#1a1a2e #888888",
                "completion-menu.meta.completion.current": "bg:#4f8dff #cccccc",
            }
        ),
    )

    # Redraw is already wired via _smart_redraw above (line 276) — it
    # auto-scrolls to bottom only when the user hasn't scrolled away.

    with create_app_session(output=create_output(stdout=None)):
        # Start the animation ticker as a background task.
        app.create_background_task(_ticker(app))

        await app.run_async()

    return 0
