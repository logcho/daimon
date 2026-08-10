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
from prompt_toolkit.layout.processors import (
    BeforeInput,
    ConditionalProcessor,
    PasswordProcessor,
)
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style

from .. import client
from ..config import Settings
from . import commands as cmd_mod
from . import display, render
from . import setup as setup_mod
from .live import LiveState
from .render import BLUE, BOLD, DIM, GREEN, RED, RESET, YELLOW, AskState

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


class ServerConfig:
    """The server's configuration, cached for display.

    Three surfaces reported the model and all three read it from a different
    place: the status bar from this process's `Settings` (frozen at launch),
    `/model` from `Settings.from_env()` (a *different* `.env` — the CLI's cwd,
    not the server's), and `/config` from the server. They disagreed the moment
    anything changed.

    Only the server can answer: it owns the file it loaded, and `POST /config`
    changes the model at runtime without touching this process at all. So
    everything reads through here, and `refresh()` is called wherever the
    configuration might just have moved.
    """

    def __init__(self, fallback: Settings) -> None:
        self._fallback = fallback
        self.data: dict = {}

    @property
    def model(self) -> str:
        """What the agent will actually use. Falls back to the launch-time
        value only until the first successful fetch."""
        return self.data.get("model") or self._fallback.model

    @property
    def context_window(self) -> int:
        return int(self.data.get("context_window") or self._fallback.context_window)

    async def refresh(self, http: Any, port: int) -> dict:
        """Re-read from the server. A failed fetch keeps the last known values
        rather than blanking the status bar."""
        cfg = await client.get_config(http, port)
        if not cfg.get("error"):
            self.data = cfg
        return cfg


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
    #: What `/skills install` is waiting to confirm.
    pending_install: dict[str, str] = {}
    #: What a `remove` is waiting to confirm — kind ("skill"/"note") and name.
    pending_delete: dict[str, str] = {}
    #: The `/setup` wizard, while one is running.
    setup_flow: setup_mod.SetupFlow | None = None
    #: The single source of truth for what's configured — see ServerConfig.
    server_config = ServerConfig(settings)

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
            input_processors=[
                BeforeInput(ANSI(f"{BLUE}❯{RESET} ")),
                # Masked only while a question asks for something secret — an
                # API key shouldn't sit on screen, but ordinary input should.
                ConditionalProcessor(
                    PasswordProcessor(char="•"),
                    Condition(lambda: state.ask is not None and state.ask.secret),
                ),
            ],
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
                    server_config.model,
                    state.stats,
                    context_window=server_config.context_window,
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
        # Local asks are confirmations and wizards handled entirely in the
        # client; only a server interrupt has something waiting to be resumed.
        # The id prefix is what tells them apart.
        ask_id = str(ask.event.get("id", ""))
        if ask_id == "install":
            await finish_install(str(value))
            return
        if ask_id == "delete":
            await finish_delete(str(value))
            return
        if ask_id.startswith(setup_mod.PREFIX):
            await advance_setup(ask_id[len(setup_mod.PREFIX):], str(value))
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

    async def show_skills(argument: str | None) -> None:
        """`/skills`, `/skills <name-or-filter>`, `/skills find …`, `/skills install …`."""
        port, _ = await client.ensure_server(settings)
        verb, _, rest = (argument or "").strip().partition(" ")
        rest = rest.strip()

        if verb == "find":
            if not rest:
                emit(["", f"  {DIM}usage:{RESET} /skills find <what it should do>"])
                return
            emit(["", f"  {DIM}searching the registry for “{rest}”…{RESET}"])
            emit(_registry_listing(await client.registry_search(http, port, rest), rest))
            return

        if verb == "install":
            await install_skill(port, rest)
            return

        if verb in ("remove", "rm", "delete"):
            await confirm_delete("skill", rest)
            return

        skills = await client.list_skills(http, port)
        if argument:
            # An exact name reads the skill; anything else filters the list —
            # which is what you want once the library outgrows one screen.
            exact = next((s for s in skills if s["name"] == argument), None)
            if exact is not None:
                emit(await _skill_detail(http, port, argument))
                return
            needle = argument.lower()
            matches = [
                s
                for s in skills
                if needle in s["name"].lower() or needle in (s.get("description") or "").lower()
            ]
            emit(_skill_listing(matches, query=argument, total=len(skills)))
            return
        emit(_skill_listing(skills))

    async def install_skill(port: int, argument: str) -> None:
        """Install from the registry, after showing what's actually in it.

        A skill is instructions the agent will follow and often scripts it may
        run, from a mostly-uncurated catalogue — so the manifest, the scripts
        and the licence go on screen and the user confirms before anything is
        written. The agent has no path to this.
        """
        if not argument:
            emit(["", f"  {DIM}usage:{RESET} /skills install <slug>"])
            return
        slug, _, chosen_path = argument.partition(" ")
        emit(["", f"  {DIM}resolving {slug}…{RESET}"])

        item = await client.registry_item(http, port, slug)
        if isinstance(item, dict) and item.get("error"):
            emit([f"  {RED}{item['error']}{RESET}"])
            return
        paths = item.get("skills") or []
        if not chosen_path:
            if len(paths) == 1:
                chosen_path = paths[0]
            else:
                # A registry entry is a repo, and a repo may hold many skills.
                emit([
                    "",
                    f"  {DIM}{item.get('repo')} contains {len(paths)} skills — "
                    f"pick one:{RESET}",
                    *(
                        f"    {BLUE}/skills install {slug} {p}{RESET}  "
                        f"{DIM}{p.rsplit('/', 1)[-1]}{RESET}"
                        for p in paths[:25]
                    ),
                ])
                return

        preview = await client.registry_preview(http, port, slug, chosen_path)
        if isinstance(preview, dict) and preview.get("error"):
            emit([f"  {RED}{preview['error']}{RESET}"])
            return

        emit(_install_preview(preview, item))
        pending_install["slug"] = slug
        pending_install["path"] = chosen_path
        pending_install["name"] = preview.get("name", slug)
        state.ask = AskState(
            event={
                "type": "ask",
                "id": "install",
                "kind": "question",
                "header": "Install",
                "question": f"Add “{preview.get('name', slug)}” to your skill library?",
                "options": [
                    {
                        "label": "Install to vault",
                        "description": "available in every project, and listed in the app",
                    },
                    {
                        "label": "Install to project",
                        "description": "lives in .daimon/skills here — only visible while "
                        "working in this project, so the app won't list it",
                    },
                    {"label": "Cancel", "description": "write nothing"},
                ],
                "multi_select": False,
            }
        )
        app.invalidate()

    async def show_notes(argument: str | None) -> None:
        """`/notes`, `/notes <name-or-filter>`, `/notes remove <name>`."""
        port, _ = await client.ensure_server(settings)
        verb, _, rest = (argument or "").strip().partition(" ")
        rest = rest.strip()

        if verb in ("remove", "rm", "delete"):
            await confirm_delete("note", rest)
            return

        notes = await client.list_notes(http, port)
        if isinstance(notes, dict) and notes.get("error"):
            emit(["", f"  {RED}{notes['error']}{RESET}"])
            return
        if argument:
            exact = next((n for n in notes if n["name"] == argument), None)
            if exact is not None:
                note = await client.read_note(http, port, argument)
                if isinstance(note, dict) and note.get("error"):
                    emit(["", f"  {RED}{note['error']}{RESET}"])
                    return
                emit([
                    "",
                    f"{DIM}── note · {argument}{RESET}",
                    display.render_markdown(str(note.get("content", ""))),
                ])
                return
            needle = argument.lower()
            notes = [n for n in notes if needle in n["name"].lower()]
        emit(_note_listing(notes, argument))

    async def confirm_delete(kind: str, name: str) -> None:
        """Deleting isn't undoable, so it always asks — through the same
        options UI the agent's own questions use."""
        if not name:
            emit(["", f"  {DIM}usage:{RESET} /{kind}s remove <name>"])
            return
        pending_delete["kind"] = kind
        pending_delete["name"] = name
        state.ask = AskState(
            event={
                "type": "ask",
                "id": "delete",
                "kind": "question",
                "header": "Delete",
                "question": f"Delete the {kind} \u201c{name}\u201d? This cannot be undone.",
                "options": [
                    {"label": "Cancel", "description": "keep it"},
                    {"label": "Delete", "description": f"remove the {kind} permanently"},
                ],
                "multi_select": False,
            }
        )
        app.invalidate()

    async def finish_delete(choice: str) -> None:
        if not choice.lower().startswith("delete"):
            emit(["", f"  {DIM}\u2298 kept{RESET}"])
            return
        port, _ = await client.ensure_server(settings)
        kind, name = pending_delete["kind"], pending_delete["name"]
        result = (
            await client.delete_skill(http, port, name)
            if kind == "skill"
            else await client.delete_note(http, port, name)
        )
        if isinstance(result, dict) and result.get("error"):
            emit(["", f"  {RED}{result['error']}{RESET}"])
            return
        emit(["", f"  {GREEN}\u2713{RESET} deleted {kind} {BLUE}{name}{RESET}"])

    async def finish_install(choice: str) -> None:
        if choice.startswith("Cancel"):
            emit(["", f"  {DIM}⊘ nothing installed{RESET}"])
            return
        scope = "project" if "project" in choice.lower() else "vault"
        port, _ = await client.ensure_server(settings)
        result = await client.install_skill(
            http, port, pending_install["slug"], pending_install["path"], scope
        )
        if isinstance(result, dict) and result.get("error"):
            emit([f"  {RED}{result['error']}{RESET}"])
            return
        files = result.get("installed") or []
        emit([
            "",
            f"  {GREEN}✓{RESET} installed {BLUE}{result.get('name')}{RESET} "
            f"{DIM}({len(files)} file{'s' if len(files) != 1 else ''}, {result.get('scope')}){RESET}",
            f"  {DIM}the agent sees it from the next turn — /skills {result.get('name')} to read it{RESET}",
        ])

    async def refresh_config() -> dict:
        """Re-read the server's configuration and redraw. Called wherever it
        might just have changed, so the status bar can't fall behind."""
        port, _ = await client.ensure_server(settings)
        cfg = await server_config.refresh(http, port)
        app.invalidate()
        return cfg

    async def show_config() -> None:
        emit(_config_lines(await refresh_config(), settings.port))

    async def show_model() -> None:
        emit(_model_lines(await refresh_config()))

    # --- the setup wizard --------------------------------------------------

    def _present(result: tuple[list[str], dict | None]) -> None:
        lines, ask = result
        emit(lines)
        state.ask = AskState(event=ask) if ask else None
        app.invalidate()

    async def start_setup() -> None:
        nonlocal setup_flow
        port, _ = await client.ensure_server(settings)
        setup_flow = setup_mod.SetupFlow(http=http, port=port)
        _present(await setup_flow.start())

    async def advance_setup(step: str, value: str) -> None:
        if setup_flow is None:
            return
        _present(await setup_flow.answer(step, value))
        # The wizard writes through POST /config, so the status bar's idea of
        # the model is stale the moment a step lands.
        await refresh_config()

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
        if name == "skills":
            parts = text.strip().split(maxsplit=1)
            app.create_background_task(
                show_skills(parts[1].strip() if len(parts) > 1 else None)
            )
            return
        if name == "config":
            app.create_background_task(show_config())
            return
        if name == "model":
            app.create_background_task(show_model())
            return
        if name == "notes":
            parts = text.strip().split(maxsplit=1)
            app.create_background_task(
                show_notes(parts[1].strip() if len(parts) > 1 else None)
            )
            return
        if name == "setup":
            app.create_background_task(start_setup())
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
            # Masking the input while it's typed is only half the job — the
            # transcript echo is permanent, so a key would sit in scrollback
            # for the rest of the session.
            shown = "•" * min(len(text), 24) if state.ask.secret else text
            emit(["", f"  {BLUE}❯{RESET} {BOLD}{shown}{RESET}"])
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

    async def first_run() -> None:
        """With no key anywhere, nothing can run — so walk the user through it
        rather than leaving them at a prompt that will only ever error."""
        try:
            # Also the first fetch that fills the status bar — until it lands,
            # the bar shows this process's launch-time guess.
            if setup_mod.needs_setup(await refresh_config()):
                await start_setup()
        except Exception:
            pass  # never block the prompt on a setup check

    scroll_to_bottom()
    app.create_background_task(ticker())
    app.create_background_task(watch_resize())
    app.create_background_task(first_run())

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


def _skill_body(text: str) -> str:
    """A SKILL.md without its frontmatter block. Mirrors the injector's
    parser, degrading to the whole text when the file doesn't have one."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


async def _skill_detail(http: Any, port: int, name: str) -> list[str]:
    """One skill, rendered."""
    skill = await client.read_skill(http, port, name)
    if skill is None:
        return ["", f"  {DIM}no skill named{RESET} {name}"]
    lines = [
        "",
        f"{DIM}── skill · {skill['name']}"
        f"{' (project)' if skill.get('source') == 'project' else ''}{RESET}",
    ]
    if skill.get("description"):
        lines.append(f"  {DIM}{skill['description']}{RESET}")
    # Strip the frontmatter — its name and description are already in the
    # header above, and rendered as markdown the `---` fences turn into a
    # horizontal rule with loose `key: value` text under it.
    lines.append(display.render_markdown(_skill_body(str(skill.get("content", "")))))
    return lines


def _registry_listing(payload: Any, query: str) -> list[str]:
    """Search results from the public registry."""
    if isinstance(payload, dict) and payload.get("error"):
        return [f"  {RED}{payload['error']}{RESET}"]
    hits = payload if isinstance(payload, list) else []
    if not hits:
        return [f"  {DIM}nothing published matches “{query}”{RESET}"]
    lines = ["", f"{DIM}── registry · {len(hits)} result(s) for “{query}”{RESET}"]
    for hit in hits:
        star = f"{BLUE}★{RESET}" if hit.get("featured") else " "
        stars = f"{hit.get('stars', 0):,}"
        lines.append(f"  {star} {BLUE}{hit['slug']}{RESET}  {DIM}{hit.get('repo','')} · {stars}★{RESET}")
        if hit.get("description"):
            lines.append(f"      {DIM}{render.truncate(hit['description'], 96)}{RESET}")
    lines.append(f"  {DIM}★ = human-curated · /skills install <slug> to add one{RESET}")
    return lines


def _install_preview(bundle: dict, item: dict) -> list[str]:
    """Everything the user should see before deciding.

    A skill is instructions the agent will follow plus, often, scripts it may
    run — so the file list flags executables, and the licence is shown because
    copying someone's files into your library is a licensing act (Anthropic's
    own skills are marked Proprietary).
    """
    files = bundle.get("files") or []
    scripts = [f for f in files if f.get("executable")]
    lines = [
        "",
        f"{DIM}── install · {bundle.get('name')}{RESET}",
        f"  {DIM}from{RESET} {item.get('url') or bundle.get('repo')} {DIM}·"
        f" {bundle.get('skill_dir')}{RESET}",
    ]
    if bundle.get("description"):
        lines.append("")
        lines.append(f"  {render.truncate(bundle['description'], 300)}")
    lines.append("")
    lines.append(f"  {DIM}{len(files)} file(s):{RESET}")
    for entry in files[:20]:
        mark = f"{YELLOW}⚙{RESET}" if entry.get("executable") else f"{DIM}·{RESET}"
        lines.append(f"    {mark} {entry['path']}")
    if len(files) > 20:
        lines.append(f"    {DIM}…and {len(files) - 20} more{RESET}")
    if scripts:
        lines.append("")
        lines.append(
            f"  {YELLOW}⚙{RESET} {DIM}{len(scripts)} script(s) — this skill can tell"
            f" the agent to run code from this repo{RESET}"
        )
    if bundle.get("license"):
        lines.append(f"  {DIM}licence:{RESET} {bundle['license']}")
    return lines


def _note_listing(notes: list, query: str | None = None) -> list[str]:
    """The agent's notes, newest first — the same set it can recall."""
    if not notes:
        return [
            "",
            f"  {DIM}no notes{' matching “' + query + '”' if query else ' yet'}"
            f" — the agent writes them as it learns things worth keeping{RESET}",
        ]
    lines = ["", f"{DIM}── notes{f' · {len(notes)} matching “{query}”' if query else ''}{RESET}"]
    for note in notes[:40]:
        size = int(note.get("sizeBytes", 0))
        lines.append(
            f"  {BLUE}{note['name']}{RESET}  {DIM}{size / 1024:.1f} KB{RESET}"
        )
    lines.append(
        f"  {DIM}/notes <name> reads one · /notes remove <name> deletes it{RESET}"
    )
    return lines


def _skill_listing(
    skills: list[dict], *, query: str | None = None, total: int | None = None
) -> list[str]:
    """The library, grouped by source. Only names and descriptions — the same
    view the agent gets, so what you see is what it's choosing from."""
    if not skills:
        if query:
            return [
                "",
                f"  {DIM}no local skill matches “{query}” — try{RESET}"
                f" {BLUE}/skills find {query}{RESET} {DIM}to search published ones{RESET}",
            ]
        return [
            "",
            f"  {DIM}no skills yet — the agent saves them as it finds reusable"
            f" procedures, or{RESET} {BLUE}/skills find <topic>{RESET}"
            f" {DIM}to install a published one{RESET}",
        ]
    heading = f"── skills · {len(skills)} of {total} matching “{query}”" if query else "── skills"
    lines = ["", f"{DIM}{heading}{RESET}"]
    for source, label in (("project", "project"), ("vault", "vault")):
        group = [s for s in skills if s.get("source") == source]
        if not group:
            continue
        lines.append(f"  {DIM}{label}{RESET}")
        for skill in group:
            description = skill.get("description") or "(no description)"
            lines.append(
                f"    {BLUE}{skill['name']}{RESET}  "
                f"{DIM}{render.truncate(description, 84)}{RESET}"
            )
    lines.append(
        f"  {DIM}/skills <name> reads one · find <topic> searches published"
        f" ones · remove <name> deletes{RESET}"
    )
    return lines


def _model_lines(cfg: dict) -> list[str]:
    """`/model` — the model settings. Pure: the caller has already fetched.

    It used to read `Settings.from_env()` in *this* process, which resolves a
    different `.env` (the CLI's cwd, not the server's) and can't see a change
    made at runtime through `POST /config`.
    """
    if cfg.get("error"):
        return ["", f"  {RED}failed to reach server:{RESET} {cfg['error']}"]

    return [
        "",
        f"{DIM}── model{RESET}",
        f"  {DIM}main{RESET}      {cfg.get('model', '?')}",
        f"  {DIM}flash{RESET}     {cfg.get('flash_model', '?')}",
        f"  {DIM}api base{RESET}  {cfg.get('api_base', '(default)')}",
        f"  {DIM}change it with{RESET} {BLUE}/setup{RESET}"
        f" {DIM}· everything else:{RESET} {BLUE}/config{RESET}",
    ]


def _config_lines(cfg: dict, port: int) -> list[str]:
    """`/config` — what the agent is actually configured with. Pure: the caller
    fetches, so this and the status bar can never be reading different things.
    Read-only — `/setup` is what changes any of it."""
    if cfg.get("error"):
        return ["", f"  {RED}failed to reach server:{RESET} {cfg['error']}"]

    lines = ["", f"{DIM}── config{RESET}", ""]

    providers = cfg.get("providers") or {}
    active_provider = str(cfg.get("model", "")).split(":")[0]
    hints: list[str] = []
    for name, info in providers.items():
        marks = [
            "✓ installed" if info.get("installed") else f"{DIM}not installed{RESET}",
            "✓ key set" if info.get("key_configured") else f"{DIM}no key{RESET}",
        ]
        active = f" {BLUE}← in use{RESET}" if name == active_provider else ""
        lines.append(f"  {DIM}{name:<12}{RESET}{' · '.join(marks)}{active}")
        # The two failure modes have different fixes, so say which applies.
        if info.get("key_configured") and not info.get("installed"):
            hints.append(
                f"  {DIM}{name} has a key but isn't installed —{RESET}"
                f" {BOLD}uv sync --extra {name}{RESET}"
            )
    lines.extend(hints)

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

    providers = cfg.get("providers") or {}
    if not any(p.get("key_configured") for p in providers.values()):
        lines += [
            "",
            f"  {BOLD}No API key set.{RESET} {DIM}Run{RESET} {BLUE}/setup{RESET}"
            f" {DIM}to get going.{RESET}",
        ]
    return lines
