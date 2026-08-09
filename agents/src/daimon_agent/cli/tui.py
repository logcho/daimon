"""The daimon CLI's interactive terminal UI.

A full-screen application: the transcript scrolls in its own pane, the live
region shows what is running right now, and the prompt and status bar stay
pinned to the bottom of the window.

**How scrolling works, and why it used to be broken.** The transcript stores
*pre-wrapped rows* — one stored row is exactly one rendered row — and the
window is set to `wrap_lines=False`. That equality is the entire design. The
original version stored logical lines (some containing embedded newlines, some
wrapping) and then computed scroll offsets from `len(lines)` while
prompt_toolkit measured the window in rendered rows. The two counted different
things, so the viewport was pinned to a position that drifted further from
reality with every turn, and no amount of adjusting the offsets could fix it.
With pre-wrapped rows, `vertical_scroll` is an exact row index and the
arithmetic cannot drift. `mouse_support` is also on, which the old version
never enabled — its wheel handler was unreachable code.

A finished line is immutable: steps are live while they run and are promoted
into the transcript when they finish, so nothing needs revising after the fact.

Imported lazily by ``main.py`` so the one-shot path stays fast.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
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
from prompt_toolkit.styles import Style

from .. import client
from ..config import Settings
from . import commands as cmd_mod
from . import display, render
from .live import LiveState
from .render import BLUE, BOLD, DIM, RESET

#: What this client tells the server it can do. "ask" is what makes the agent's
#: ask_user/present_plan tools available at all.
CAPABILITIES = ["ask"]


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


#: How many logical lines of history to keep. Old output is dropped rather
#: than re-wrapped forever; the transcript is a view, not the record (the
#: checkpointer holds the actual conversation).
MAX_TRANSCRIPT_LINES = 5000


class Transcript:
    """Finalized output and the viewport onto it.

    Holds both the logical lines and their wrapped rows. Everything downstream
    counts rows, so `vertical_scroll` is an exact index into `rows` and the
    scroll arithmetic is trivially correct — see the module docstring for what
    happens when it isn't.
    """

    def __init__(self, width: int) -> None:
        self.width = width
        self._lines: list[str] = []
        self.rows: list[str] = []
        #: Stick to the bottom as new output arrives. Cleared by scrolling up,
        #: restored by scrolling back down or starting a turn.
        self.follow = True

    def append(self, lines: list[str]) -> None:
        self._lines.extend(lines)
        if len(self._lines) > MAX_TRANSCRIPT_LINES:
            self._lines = self._lines[-MAX_TRANSCRIPT_LINES:]
            self.rows = render.wrap_all(self._lines, self.width)
            return
        for line in lines:
            self.rows.extend(render.wrap_ansi(line, self.width))

    def clear(self) -> None:
        self._lines.clear()
        self.rows.clear()
        self.follow = True

    def set_width(self, width: int) -> None:
        """Re-wrap on resize. The stored logical lines are the source of truth;
        rows are derived, so a resize just recomputes them."""
        if width == self.width:
            return
        self.width = width
        self.rows = render.wrap_all(self._lines, width)

    def text(self) -> str:
        return "\n".join(self.rows)


class _StderrToTranscript:
    """Routes `sys.stderr` into the transcript while the app owns the screen.

    A full-screen application draws the whole terminal, so anything else that
    writes to it tears the display — the client's own notices ("server
    workspace mismatch — restarting") landed on top of the prompt. Capturing
    them turns a corrupted screen into an ordinary transcript line, which is
    also where the user would look for them.

    Line-buffered: a partial write is held until its newline arrives, so a
    message assembled from several `print` calls stays on one line.
    """

    def __init__(self, emit, original) -> None:
        self._emit = emit
        self._original = original
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._emit([f"  {DIM}{line.strip()}{RESET}"])
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        # Rich and friends ask; the transcript renders ANSI, so say yes.
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


async def run_tui(
    settings: Settings,
    session_name: str | None,
    http: Any,  # aiohttp.ClientSession
) -> int:
    """Launch the interactive TUI. Returns 0 on clean exit."""
    width = max(shutil.get_terminal_size((80, 24)).columns - 2, 40)
    state = LiveState(width=width)
    transcript = Transcript(width)
    transcript.append(display.render_banner(session_name))
    session_id = session_name or "cli"
    mode = "normal"
    turn_task: asyncio.Task | None = None
    turn_started = 0.0

    # --- layout ------------------------------------------------------------

    # One stored row == one rendered row, so `wrap_lines=False` and
    # `vertical_scroll` is an exact row offset. The cursor is parked on the
    # first visible row so prompt_toolkit's scroll-to-cursor logic always finds
    # it already visible and leaves our offset alone.
    transcript_window = Window(
        content=FormattedTextControl(
            text=lambda: ANSI(transcript.text()),
            focusable=False,
            get_cursor_position=lambda: Point(
                0, min(transcript_window.vertical_scroll, max(len(transcript.rows) - 1, 0))
            ),
        ),
        wrap_lines=False,
        always_hide_cursor=True,
        right_margins=[ScrollbarMargin(display_arrows=True)],
    )

    def scroll_to_bottom() -> None:
        """Overshoot deliberately: prompt_toolkit clamps `vertical_scroll` to
        the true maximum during render, which is the only place the exact
        window height is known."""
        transcript.follow = True
        transcript_window.vertical_scroll = len(transcript.rows)

    def page_size() -> int:
        info = transcript_window.render_info
        return max((info.window_height if info else 20) - 2, 1)

    def scroll_by(rows: int) -> None:
        info = transcript_window.render_info
        height = info.window_height if info else 20
        maximum = max(len(transcript.rows) - height, 0)
        target = transcript_window.vertical_scroll + rows
        if target >= maximum:
            scroll_to_bottom()
            return
        transcript.follow = False
        transcript_window.vertical_scroll = max(target, 0)

    live_window = Window(
        content=FormattedTextControl(
            text=lambda: ANSI("\n".join(state.lines())), focusable=False
        ),
        # The live region grows and shrinks with its content; `dont_extend_
        # height` is what keeps the application from claiming the whole screen
        # and turning this back into a full-screen app by accident.
        height=Dimension(min=0),
        dont_extend_height=True,
        # Streaming text arrives as one long line until a newline lands, so it
        # has to wrap — truncating would hide the answer as it was written.
        wrap_lines=True,
        always_hide_cursor=True,
    )

    input_buffer = Buffer(
        multiline=True,
        completer=_SlashCompleter(),
        complete_while_typing=True,
        history=InMemoryHistory(),
    )
    input_window = Window(
        content=BufferControl(
            buffer=input_buffer,
            input_processors=[BeforeInput(ANSI(f"{BLUE}❯{RESET} "))],
        ),
        height=Dimension(min=1),
        dont_extend_height=True,
        wrap_lines=True,
    )

    status_window = Window(
        content=FormattedTextControl(
            text=lambda: ANSI(
                render.status_line(
                    session_id,
                    settings.model,
                    state.stats,
                    context_window=settings.context_window,
                    mode=mode,
                )
            ),
            focusable=False,
        ),
        height=1,
        dont_extend_height=True,
    )

    divider = Window(height=1, char="─", style="class:divider")

    body = FloatContainer(
        content=HSplit([
            transcript_window,   # flexes to fill; scrolls
            live_window,         # in-flight work, sized to content
            divider,
            input_window,        # pinned above the status bar
            status_window,
        ]),
        floats=[
            Float(
                xcursor=True,
                ycursor=True,
                transparent=True,
                content=CompletionsMenu(max_height=12, scroll_offset=1, display_arrows=True),
            )
        ],
    )

    app = Application(
        layout=Layout(body, focused_element=input_window),
        key_bindings=(kb := KeyBindings()),
        full_screen=True,
        # On, so the wheel scrolls the transcript. The old version left this
        # off, which made its wheel handler dead code — half of why scrolling
        # never worked. Shift+drag still selects text in most terminals.
        mouse_support=True,
        style=Style.from_dict(
            {
                "divider": "#374151",
                "scrollbar.background": "bg:#1a1a2e",
                "scrollbar.button": "bg:#4b5563",
                "completion-menu": "bg:#1a1a2e #e5e5e5",
                "completion-menu.completion.current": "bg:#4f8dff #ffffff",
                "completion-menu.meta.completion": "bg:#1a1a2e #888888",
                "completion-menu.meta.completion.current": "bg:#4f8dff #cccccc",
            }
        ),
    )

    # The built-in wheel handler moves `vertical_scroll` but knows nothing
    # about following the bottom, so wrap it to keep the two in agreement.
    _inner_mouse_handler = transcript_window._mouse_handler

    def _mouse(mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            transcript.follow = False
        result = _inner_mouse_handler(mouse_event)
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            info = transcript_window.render_info
            height = info.window_height if info else 20
            if transcript_window.vertical_scroll >= max(len(transcript.rows) - height, 0):
                transcript.follow = True
        return result

    transcript_window._mouse_handler = _mouse

    def emit(lines: list[str]) -> None:
        if lines:
            transcript.append(lines)
            if transcript.follow:
                scroll_to_bottom()
            app.invalidate()

    def on_event(event: dict) -> None:
        emit(state.consume(event))
        app.invalidate()

    # --- turns -------------------------------------------------------------

    async def run_turn(text: str) -> None:
        nonlocal turn_started
        turn_started = time.monotonic()
        state.start_turn()
        emit(render.user_line(text))
        try:
            port, _ = await client.ensure_server(settings)
            terminal = await client.stream_turn(
                http, port, session_id, text, on_event,
                capabilities=CAPABILITIES, mode=mode,
            )
            await _settle(terminal)
        except client.ClientError as exc:
            emit(state.end_turn() + render.error_lines(str(exc)))
        except asyncio.CancelledError:
            emit(state.cancel())
            raise
        finally:
            app.invalidate()

    async def answer(value: Any) -> None:
        """Send the user's answer and keep streaming the same turn."""
        ask = state.ask
        state.ask = None
        app.invalidate()
        if ask is None:
            return
        try:
            port, _ = await client.ensure_server(settings)
            terminal = await client.resume_turn(
                http, port, session_id, str(ask.event.get("id", "")), value, on_event
            )
            await _settle(terminal)
        except client.ClientError as exc:
            emit(state.end_turn() + render.error_lines(str(exc)))
        except asyncio.CancelledError:
            emit(state.cancel())
            raise
        finally:
            app.invalidate()

    async def _settle(terminal: dict) -> None:
        """Close out whatever the stream ended on."""
        if terminal.get("type") == "ask":
            return  # the live region now shows the question; wait for a key
        streamed = state.streamed_any
        promoted = state.end_turn()
        if terminal.get("type") == "done":
            result = str(terminal.get("result") or "")
            # `done` repeats the whole answer for clients that ignore deltas.
            # Here the deltas already put it on screen, so re-rendering it
            # would print the reply twice; the markdown pass is only for a
            # turn that produced no stream at all.
            if result and not streamed:
                promoted.append(display.render_markdown(result))
            state.stats.turns += 1
            elapsed = time.monotonic() - turn_started
            state.stats.last_elapsed = elapsed
            promoted.append(render.turn_footer(state.stats, elapsed))
        emit(promoted)

    def start_turn(text: str) -> None:
        nonlocal turn_task
        turn_task = app.create_background_task(run_turn(text))

    def start_answer(value: Any) -> None:
        nonlocal turn_task
        scroll_to_bottom()
        turn_task = app.create_background_task(answer(value))

    def cancel_turn() -> bool:
        """Cancel the in-flight turn. Aborting the request also drops the
        connection, which the server reads as 'client gone' and uses to cancel
        the work rather than let an abandoned turn keep burning tokens."""
        nonlocal turn_task
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
            turn_task = None
            return True
        return False

    # --- slash commands ----------------------------------------------------

    async def show_tools() -> None:
        port, _ = await client.ensure_server(settings)
        tools = await client.list_tools(http, port)
        if not tools:
            emit(["", f"  {DIM}no tools found — is the agent server running?{RESET}"])
            return
        emit(_tool_listing(tools))

    async def show_setup(key: str | None) -> None:
        emit(await _setup_lines(settings, http, key))

    def handle_slash(text: str) -> None:
        nonlocal mode
        raw = text[1:].strip()
        name = raw.split(maxsplit=1)[0].lower() if raw else ""
        name = _ALIASES.get(name, name)

        if name == "exit":
            app.exit()
            return
        if name == "clear":
            transcript.clear()
            transcript.append(display.render_banner(session_name))
            scroll_to_bottom()
            return
        if name == "plan":
            mode = "normal" if mode == "plan" else "plan"
            emit([
                "",
                f"  {DIM}plan mode {'on — I\'ll present a plan before changing anything' if mode == 'plan' else 'off'}{RESET}",
            ])
            return
        if name == "tools":
            app.create_background_task(show_tools())
            return
        if name == "setup":
            parts = text.strip().split(maxsplit=1)
            key = parts[1].strip() if len(parts) > 1 else None
            if key and not (key.startswith("sk-") or len(key) >= 20):
                key = None
            app.create_background_task(show_setup(key))
            return

        commands = cmd_mod.get_commands()
        if name in commands:
            emit(commands[name][1](text, session_name))
        else:
            emit([
                "",
                f"  {DIM}unknown:{RESET} {text}",
                f"  {DIM}type{RESET} {BLUE}/help{RESET} {DIM}for commands{RESET}",
            ])

    # --- key bindings ------------------------------------------------------

    asking = Condition(lambda: state.ask is not None)
    picking = Condition(lambda: state.ask is not None and not state.ask.freeform)
    completing = Condition(lambda: input_buffer.complete_state is not None)

    @kb.add("c-c")
    def _interrupt(event) -> None:
        """Cancel the turn if one is running; exit if idle. Ctrl-C used to kill
        the whole TUI mid-turn, which threw away the session to stop one task."""
        if state.ask is not None:
            state.ask = None
            emit(["", f"  {DIM}⊘ skipped{RESET}"])
            return
        if cancel_turn():
            return
        if input_buffer.text:
            input_buffer.reset()
            return
        event.app.exit()

    @kb.add("c-d")
    def _eof(event) -> None:
        if not input_buffer.text:
            event.app.exit()

    # Not `eager`: Escape is also the prefix of Alt+Enter (`escape enter`), and
    # firing this immediately would swallow the newline binding. prompt_toolkit
    # waits out its escape timeout before deciding Escape stood alone, which
    # costs cancel a few tens of milliseconds and keeps Alt+Enter working.
    @kb.add("escape", filter=~completing)
    def _escape(event) -> None:
        if state.ask is not None:
            state.ask = None
            emit(["", f"  {DIM}⊘ skipped{RESET}"])
            app.invalidate()
            return
        cancel_turn()

    # ---- answering a question ----

    @kb.add("up", filter=picking)
    def _ask_up(event) -> None:
        state.ask.cursor = max(0, state.ask.cursor - 1)

    @kb.add("down", filter=picking)
    def _ask_down(event) -> None:
        state.ask.cursor = min(len(state.ask.options) - 1, state.ask.cursor + 1)

    @kb.add("space", filter=picking)
    def _ask_toggle(event) -> None:
        ask = state.ask
        if not ask.multi:
            return
        ask.selected.symmetric_difference_update({ask.cursor})

    for _digit in "123456789":
        @kb.add(_digit, filter=picking)
        def _ask_digit(event, _d=_digit) -> None:
            ask = state.ask
            index = int(_d) - 1
            if index >= len(ask.options):
                return
            ask.cursor = index
            if ask.multi:
                ask.selected.symmetric_difference_update({index})
            else:
                start_answer(ask.options[index]["label"])

    @kb.add("e", filter=picking)
    def _ask_freeform(event) -> None:
        """Answer in your own words rather than picking — the "Other" escape
        hatch, because a four-option list never covers everything."""
        state.ask.freeform = True
        input_buffer.reset()

    @kb.add("enter", filter=picking)
    def _ask_confirm(event) -> None:
        ask = state.ask
        if ask.multi:
            chosen = [ask.options[i]["label"] for i in sorted(ask.selected)]
            start_answer(chosen or [ask.options[ask.cursor]["label"]])
        elif ask.options:
            start_answer(ask.options[ask.cursor]["label"])
        else:
            state.ask.freeform = True

    # ---- the prompt ----

    # ---- scrolling the transcript ----

    @kb.add("pageup")
    def _page_up(event) -> None:
        scroll_by(-page_size())

    @kb.add("pagedown")
    def _page_down(event) -> None:
        scroll_by(page_size())

    @kb.add("c-home")
    def _scroll_top(event) -> None:
        transcript.follow = False
        transcript_window.vertical_scroll = 0

    @kb.add("c-end")
    def _scroll_end(event) -> None:
        scroll_to_bottom()

    @kb.add("s-up")
    def _line_up(event) -> None:
        scroll_by(-1)

    @kb.add("s-down")
    def _line_down(event) -> None:
        scroll_by(1)

    @kb.add("tab")
    def _tab(event) -> None:
        buf = event.app.current_buffer
        if buf.complete_state:
            buf.complete_next()
        else:
            buf.start_completion(select_first=True)

    @kb.add("up", filter=completing)
    def _complete_up(event) -> None:
        input_buffer.complete_previous()

    @kb.add("down", filter=completing)
    def _complete_down(event) -> None:
        input_buffer.complete_next()

    @kb.add("up", filter=~completing & ~picking)
    def _history_up(event) -> None:
        """Previous input. Scrolling is the terminal's job now, so the arrows
        are free for what a shell prompt uses them for."""
        input_buffer.history_backward()

    @kb.add("down", filter=~completing & ~picking)
    def _history_down(event) -> None:
        input_buffer.history_forward()

    @kb.add("escape", "enter")
    def _newline(event) -> None:
        event.app.current_buffer.insert_text("\n")

    @kb.add("enter", filter=~picking)
    def _submit(event) -> None:
        buf = event.app.current_buffer
        if buf.complete_state and buf.complete_state.current_completion:
            buf.apply_completion(buf.complete_state.current_completion)
            return
        text = buf.text.strip()
        if not text:
            return
        buf.reset()
        buf.history.append_string(text)
        # Submitting means the user is at the prompt, not reading scrollback,
        # so jump back to the bottom. Output arriving while they *are* reading
        # deliberately doesn't move the view — that's what `follow` is for.
        scroll_to_bottom()

        if asking():
            emit(["", f"  {BLUE}❯{RESET} {BOLD}{text}{RESET}"])
            start_answer(text)
            return
        if text.startswith("/"):
            handle_slash(text)
            return
        if state.busy:
            emit(["", f"  {DIM}a turn is already running — esc to cancel it{RESET}"])
            return
        start_turn(text)

    # --- animation ---------------------------------------------------------

    async def ticker() -> None:
        """Drive the spinner and the elapsed clock. Only invalidates while
        something is actually in flight — an idle prompt costs nothing."""
        while True:
            await asyncio.sleep(render.SPINNER_INTERVAL)
            if not state.busy:
                continue
            state.tick(render.SPINNER_INTERVAL)
            app.invalidate()

    async def watch_resize() -> None:
        """Re-wrap the transcript when the terminal changes width. The stored
        logical lines are the source of truth, so this is a recompute rather
        than a reflow of anything already on screen."""
        nonlocal width
        while True:
            await asyncio.sleep(0.5)
            new_width = max(shutil.get_terminal_size((80, 24)).columns - 2, 40)
            if new_width != width:
                width = new_width
                state.width = new_width
                transcript.set_width(new_width)
                if transcript.follow:
                    scroll_to_bottom()
                app.invalidate()

    # --- launch ------------------------------------------------------------

    scroll_to_bottom()
    app.create_background_task(ticker())
    app.create_background_task(watch_resize())

    # Nothing but the app may write to the terminal while it is drawing.
    saved_stderr = sys.stderr
    sys.stderr = _StderrToTranscript(emit, saved_stderr)
    try:
        await app.run_async()
    finally:
        sys.stderr = saved_stderr
    return 0


_ALIASES = {
    "h": "help",
    "?": "help",
    "cls": "clear",
    "st": "status",
    "quit": "exit",
    "q": "exit",
    "ws": "workspace",
}



_TOOL_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("file ops", ("read_file", "write_file", "edit_file", "mkdir", "list_directory",
                  "delete_file", "move_file", "glob_files", "grep_files")),
    ("execution", ("kernel_execute", "run_shell")),
    ("code quality", ("check_code", "debug", "run_tests")),
    ("search", ("web_search", "web_fetch")),
    ("browser", ("open_url", "read_page", "click", "fill_field", "new_tab",
                 "switch_tab", "close_tab", "extract_text")),
    ("memory & skills", ("recall", "list_skills", "read_skill", "save_skill")),
    ("planning", ("update_todos", "ask_user", "present_plan")),
    ("delegation", ("research", "task")),
    ("host", ("stage_terminal_command",)),
]


def _tool_listing(tools: list[dict]) -> list[str]:
    """Group the server's tool list by category for display."""
    known = {name for _, names in _TOOL_CATEGORIES for name in names}
    groups: dict[str, list[str]] = {}
    for tool in tools:
        name = tool.get("name", "")
        category = next(
            (label for label, names in _TOOL_CATEGORIES if name in names),
            "other" if name not in known else "other",
        )
        summary = " ".join(str(tool.get("description", "")).split())
        groups.setdefault(category, []).append(
            f"    {BLUE}{name}{RESET}  {DIM}{render.truncate(summary, 92)}{RESET}"
        )

    lines = ["", f"{DIM}── tools{RESET}"]
    for label, _ in [*_TOOL_CATEGORIES, ("other", ())]:
        if label in groups:
            lines.append(f"  {DIM}{label}{RESET}")
            lines.extend(groups[label])
    lines.append(f"  {DIM}{len(tools)} tools{RESET}")
    return lines


async def _setup_lines(settings: Settings, http: Any, key: str | None) -> list[str]:
    """`/setup` — read (and optionally write) config through the server, so
    what's shown is what the agent actually has, not what this process guessed."""
    from .render import RED

    port, _ = await client.ensure_server(settings)
    base = f"http://127.0.0.1:{port}/config"
    try:
        if key:
            async with http.post(base, json={"api_key": key}) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return ["", f"  {RED}failed to save key:{RESET} {body[:200]}"]
        async with http.get(base) as resp:
            if resp.status != 200:
                return ["", f"  {RED}server returned {resp.status}{RESET}"]
            cfg = await resp.json()
    except Exception as exc:
        return ["", f"  {RED}failed to reach server:{RESET} {exc}"]

    lines = ["", f"{DIM}── setup{RESET}", ""]
    if key:
        lines.append(f"  {DIM}api key{RESET}      ✓ {DIM}saved to .env, in effect now{RESET}")
        lines.append("")

    providers = cfg.get("providers") or {}
    for name, info in providers.items():
        marks = []
        marks.append("✓ installed" if info.get("installed") else f"{DIM}not installed{RESET}")
        marks.append("✓ key set" if info.get("key_configured") else f"{DIM}no key{RESET}")
        active = " ←" if name == cfg.get("provider") else ""
        lines.append(f"  {DIM}{name:<12}{RESET}{' · '.join(marks)}{active}")

    lines.append("")
    lines.append(f"  {DIM}model{RESET}        {cfg.get('model', '?')}")
    flash = cfg.get("flash_model")
    if flash and flash != cfg.get("model"):
        lines.append(f"  {DIM}flash{RESET}        {flash}")
    lines.append(f"  {DIM}workspace{RESET}    {cfg.get('workspace', '?')}")
    lines.append(f"  {DIM}api base{RESET}     {cfg.get('api_base', '(default)')}")
    pinch = "✓ " + str(cfg.get("pinchtab_base", "")) if cfg.get("pinchtab_healthy") else (
        f"{DIM}not configured{RESET}"
    )
    lines.append(f"  {DIM}pinchtab{RESET}     {pinch}")
    lines.append(f"  {DIM}max tokens{RESET}   {cfg.get('max_tokens', '?')}")
    lines.append(f"  {DIM}config{RESET}       .env {DIM}(port {port}){RESET}")

    if not cfg.get("api_key_configured"):
        lines += [
            "",
            f"  {BOLD}To get started:{RESET}",
            f"  1. Get an API key → {BLUE}https://platform.deepseek.com/api_keys{RESET}",
            f"  2. Paste it here:  {BOLD}/setup sk-your-key-here{RESET}",
        ]
    return lines
