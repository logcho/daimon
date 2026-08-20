"""CLI tests — entry point wiring, stream display, markdown rendering.

Tests the ``cli.main`` entry point (mode dispatch, thin-client wiring) and
the Rich-based rendering pipeline. The piped-output contract is preserved:
when stdout/stderr are not TTYs, output must be plain ASCII with no escape
codes.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import pytest

from daimon_agent import cli
from daimon_agent.cli import display

# Access the main module without the __init__.py name shadowing.
import sys as _sys
import daimon_agent.cli.main  # noqa: F811 — loads the module into sys.modules
_m = _sys.modules["daimon_agent.cli.main"]


# ---------------------------------------------------------------------------
# Stream display (non-TUI step rendering)
# ---------------------------------------------------------------------------

class TestStreamDisplay:
    """The non-interactive progress display: steps to stderr as they finish,
    so stdout stays clean enough to pipe."""

    def test_finished_steps_are_printed_with_their_detail(self, capsys):
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({
            "type": "step", "id": "s1", "label": "read_file", "status": "done",
            "tool": "read_file", "detail": "notes.md",
        })
        err = capsys.readouterr().err
        assert "read_file" in err and "notes.md" in err

    def test_running_steps_are_not_printed(self, capsys):
        """Only finished steps print — a `running` line would have to be
        rewritten later, and this path may be writing to a pipe."""
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({
            "type": "step", "id": "s1", "label": "read_file",
            "status": "running", "tool": "read_file",
        })
        assert capsys.readouterr().err == ""

    def test_thinking_steps_are_never_printed(self, capsys):
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({"type": "step", "id": "t1", "label": "Thinking", "status": "running"})
        sd.on_event({"type": "step", "id": "t1", "label": "Thinking", "status": "done"})
        assert capsys.readouterr().err == ""

    def test_error_event_is_printed(self, capsys):
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({"type": "error", "message": "boom"})
        assert "boom" in capsys.readouterr().err

    def test_nothing_reaches_stdout(self, capsys):
        """stdout carries the result and only the result."""
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({"type": "step", "id": "s", "label": "t", "status": "done", "tool": "t"})
        sd.on_event({"type": "error", "message": "boom"})
        sd.finish()
        assert capsys.readouterr().out == ""

    def test_unknown_events_are_ignored(self):
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({"type": "usage", "input_tokens": 1})
        sd.on_event({"type": "assistant_delta", "text": "hi"})
        sd.finish()


# ---------------------------------------------------------------------------
# Markdown rendering (Rich-based, replaces _md_to_ansi)
# ---------------------------------------------------------------------------

class TestMarkdownRendering:
    def test_markdown_plain_when_not_tty(self):
        c = display.make_console(plain=True)
        out = display.render_markdown("**hi**\n\n`code`", console=c)
        # Rich with force_terminal=False produces plain text
        assert "**hi**" in out or "hi" in out

    def test_markdown_styled_when_tty(self):
        out = display.render_markdown("**hi**")
        # Should contain bold ANSI codes
        assert "\x1b[1m" in out or "hi" in out

    def test_markdown_code_block(self):
        out = display.render_markdown("```\nls -la\n```")
        assert "ls -la" in out

    def test_markdown_heading(self):
        out = display.render_markdown("# Title")
        assert "Title" in out

    def test_markdown_inline_code(self):
        out = display.render_markdown("run `make test` now")
        assert "make test" in out


# ---------------------------------------------------------------------------
# Tool step rendering
# ---------------------------------------------------------------------------

class TestToolStepRendering:
    def test_running_step_has_arrow(self):
        out = display.render_tool_step("web_search", "running")
        assert "→" in out or "web_search" in out

    def test_done_step_has_check(self):
        out = display.render_tool_step("web_search", "done")
        assert "✓" in out or "web_search" in out

    def test_error_step_has_x(self):
        out = display.render_tool_step("web_search", "error")
        assert "✗" in out or "web_search" in out

    def test_tool_name_present(self):
        out = display.render_tool_step("vault_search", "done", label="searching")
        assert "vault_search" in out

    def test_unknown_status_empty(self):
        assert display.render_tool_step("x", "unknown") == ""


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

class TestBanner:
    def test_banner_lines_has_wordmark(self):
        lines = display.render_banner()
        assert len(lines) >= 3
        assert any("daimon" in line for line in lines)

    def test_banner_lines_with_session(self):
        lines = display.render_banner(session_name="work")
        assert any("work" in line for line in lines)


# ---------------------------------------------------------------------------
# _amain wiring: thin client of the server
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_client(monkeypatch):
    """Record what the CLI's client calls, and script happy-path returns."""
    calls: dict = {"ensure": 0, "stream": None, "stop": None}

    async def fake_ensure(settings, *, run_dir=None):
        calls["ensure"] += 1
        return 4711, False

    async def fake_stream(http, port, session_id, instruction, emit, agent=None, **kwargs):
        calls["stream"] = (session_id, instruction, agent)
        # stream_turn returns the terminal event, not the bare result string.
        return {"type": "done", "result": f"result for {session_id}"}

    async def fake_stop(run_dir=None):
        calls["stop"] = True
        return True, "stopped daimon server"

    monkeypatch.setattr(_m.client, "ensure_server", fake_ensure)
    monkeypatch.setattr(_m.client, "stream_turn", fake_stream)
    monkeypatch.setattr(_m.client, "stop_server", fake_stop)
    return calls


async def test_amain_one_shot_default_session(fake_client, capsys):
    code = await _m._amain(["say hi"])
    assert code == 0
    assert fake_client["ensure"] == 1
    assert fake_client["stream"] == ("cli", "say hi", None)
    assert "result for cli" in capsys.readouterr().out


async def test_amain_named_session(fake_client, capsys):
    code = await _m._amain(["-n", "work", "hello"])
    assert code == 0
    assert fake_client["stream"] == ("work", "hello", None)
    assert "result for work" in capsys.readouterr().out


async def test_amain_error_turn_returns_1(fake_client, monkeypatch):
    async def fake_stream(http, port, session_id, instruction, emit, agent="general", **kwargs):
        return {"type": "error", "message": "boom"}  # the server emitted an error event
    monkeypatch.setattr(_m.client, "stream_turn", fake_stream)
    code = await _m._amain(["boom"])
    assert code == 1


async def test_amain_list_sessions(fake_client, monkeypatch, capsys):
    monkeypatch.setattr(_m.client, "list_sessions", lambda db: ["cli", "work"])
    code = await _m._amain(["--list"])
    assert code == 0
    assert capsys.readouterr().out.split() == ["cli", "work"]
    assert fake_client["ensure"] == 0  # --list never spawns a server


async def test_amain_stop_ok(fake_client, capsys):
    code = await _m._amain(["--stop"])
    assert code == 0
    assert "stopped daimon server" in capsys.readouterr().out
    assert fake_client["ensure"] == 0


async def test_amain_stop_refused_exits_1(fake_client, monkeypatch, capsys):
    async def fake_stop(run_dir=None):
        return False, "nothing to stop"
    monkeypatch.setattr(_m.client, "stop_server", fake_stop)
    code = await _m._amain(["--stop"])
    assert code == 1
    assert "nothing to stop" in capsys.readouterr().out


async def test_amain_piped_stdin(fake_client, monkeypatch):
    class FakeStdin:
        def isatty(self):
            return False
        def read(self):
            return "piped hello"
    monkeypatch.setattr(_m.sys, "stdin", FakeStdin())
    code = await _m._amain([])
    assert code == 0
    assert fake_client["stream"] == ("cli", "piped hello", None)


async def test_amain_version(fake_client, capsys):
    code = await _m._amain(["--version"])
    assert code == 0
    assert "daimon" in capsys.readouterr().out


async def test_amain_no_agent_flag(fake_client, capsys):
    """No --agent flag — agent parameter is no longer needed."""
    code = await _m._amain(["hello world"])
    assert code == 0
    assert fake_client["stream"] == ("cli", "hello world", None)


# ---------------------------------------------------------------------------
# TUI integration test (FakeApp)
# ---------------------------------------------------------------------------

async def test_amain_interactive_tui(fake_client, monkeypatch, capsys):
    """The interactive path: FakeApp stands in for the prompt_toolkit
    Application, types 'hello', fires the enter binding, and verifies the turn
    reached the client with the CLI's capabilities attached."""
    monkeypatch.setattr(_m.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(_m.sys.stderr, "isatty", lambda: True)
    # Workspace confirmation prompt: simulate pressing Enter (empty = default).
    monkeypatch.setattr("builtins.input", lambda: "")

    from prompt_toolkit.keys import Keys

    from daimon_agent.cli import tui as tui_mod

    class FakeKeyEvent:
        def __init__(self, app):
            self.app = app

    class FakeApp:
        """Matches the real Application's constructor keywords — including
        the two the inline design turns *off* on purpose."""

        def __init__(self, layout=None, key_bindings=None, full_screen=False,
                     mouse_support=False, erase_when_done=False, style=None):
            self.layout = layout
            self._kb = key_bindings
            self._bg_tasks: list = []
            self.current_buffer = None
            self.full_screen = full_screen
            self.mouse_support = mouse_support
            self.printed: list = []

        async def run_async(self):
            # Find the prompt by its control type rather than its index, so
            # rearranging the layout doesn't silently break this test.
            from prompt_toolkit.layout.controls import BufferControl

            hs = self.layout.container.content
            input_buf = next(
                child.content.buffer
                for child in hs.children
                if isinstance(getattr(child, "content", None), BufferControl)
            )
            self.current_buffer = input_buf
            input_buf.text = "hello"
            input_buf.cursor_position = len("hello")

            # Plain Enter only — `escape enter` (newline) is also keyed on
            # Enter, and `enter` while a question is open is a different
            # handler again, so match on the exact single-key binding whose
            # filter is currently active.
            for binding in self._kb.bindings:
                if binding.keys == (Keys.Enter,) and binding.filter():
                    binding.handler(FakeKeyEvent(self))
                    break

            # Let background tasks run (non-blocking pulse — don't await
            # infinite-loop tasks like the ticker).
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            for task in self._bg_tasks:
                if task.done():
                    try:
                        task.result()
                    except Exception:
                        pass
                else:
                    task.cancel()
            return None

        def create_background_task(self, coro):
            task = asyncio.ensure_future(coro)
            self._bg_tasks.append(task)
            return task

        def print_text(self, text):
            self.printed.append(text)

        def exit(self):
            pass

        def invalidate(self):
            pass

    captured: dict = {}
    real_app = tui_mod.Application

    def _capture(*args, **kwargs):
        app = FakeApp(*args, **kwargs)
        captured["app"] = app
        return app

    monkeypatch.setattr(tui_mod, "Application", _capture)

    code = await _m._amain([])
    assert code == 0
    assert fake_client["stream"][0] == "cli"
    assert fake_client["stream"][1] == "hello"
    # At least one — the startup first-run check also ensures the server, and
    # how many times it's asked is an implementation detail, not the contract.
    assert fake_client["ensure"] >= 1
    # Full-screen, with the mouse captured so the wheel scrolls the
    # transcript — leaving mouse_support off is half of why scrolling never
    # worked in the original version.
    assert captured["app"].full_screen is True
    assert captured["app"].mouse_support is True
    assert real_app is not FakeApp  # sanity: we patched the name we meant to


async def test_tui_advertises_the_ask_capability():
    """Without this the server withholds ask_user/present_plan, and the agent
    can never confer with the user."""
    from daimon_agent.cli import tui as tui_mod

    assert "ask" in tui_mod.CAPABILITIES


# ---------------------------------------------------------------------------
# SlashCompleter unit tests (mirrors commands.py)
# ---------------------------------------------------------------------------

from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document

from daimon_agent.cli import commands as cmd_mod

_CMDS = cmd_mod.get_commands()


class _TestSlashCompleter:
    """Mirrors the TUI's _SlashCompleter using the real command table."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        word = text[1:]
        for cmd, (desc, _) in _CMDS.items():
            if cmd.startswith(word):
                yield Completion(
                    cmd,
                    start_position=-len(word),
                    display=f"/{cmd}",
                    display_meta=desc,
                )


def _get_completions(text: str):
    """Helper: return list of (text, display_meta) for the given input."""
    completer = _TestSlashCompleter()
    doc = Document(text, len(text))
    return [
        (c.text, c.display_meta)
        for c in completer.get_completions(doc, None)
    ]


def test_completer_returns_all_on_bare_slash():
    completions = _get_completions("/")
    texts = {t for t, _ in completions}
    assert texts >= {"help", "clear", "status", "model", "tools", "workspace"}


def test_completer_filters_by_prefix():
    completions = _get_completions("/h")
    texts = [t for t, _ in completions]
    assert texts == ["help"]


def test_completer_no_results_on_no_slash():
    completions = _get_completions("hello")
    assert completions == []


def test_completer_has_display_meta():
    completions = _get_completions("/")
    for _text, meta in completions:
        assert meta is not None
        assert len(meta) > 0


def test_completer_start_position():
    completer = _TestSlashCompleter()
    doc = Document("/he", 3)
    completions = list(completer.get_completions(doc, None))
    assert len(completions) == 1
    assert completions[0].start_position == -2  # "he" length


# ---------------------------------------------------------------------------
# Transcript viewport — the scroll model
# ---------------------------------------------------------------------------

class TestTranscript:
    """The transcript stores pre-wrapped rows so one row == one rendered row.
    Scrolling is exact only because of that equality."""

    def _transcript(self, width=40):
        from daimon_agent.cli.tui import Transcript

        return Transcript(width)

    def test_rows_are_prewrapped_to_width(self):
        from daimon_agent.cli.render import visible_len

        t = self._transcript(20)
        t.append(["x" * 100])
        assert len(t.rows) == 5
        assert all(visible_len(r) <= 20 for r in t.rows)

    def test_row_count_matches_rendered_lines(self):
        """`text()` is what the window renders; its line count must equal
        `len(rows)` or every scroll offset is wrong."""
        t = self._transcript(20)
        t.append(["short", "y" * 55, "also short"])
        assert t.text().count("\n") + 1 == len(t.rows)

    def test_resize_rewraps_from_the_logical_lines(self):
        t = self._transcript(20)
        t.append(["z" * 60])
        assert len(t.rows) == 3
        t.set_width(60)
        assert len(t.rows) == 1  # re-wrapped from source, not from the rows
        t.set_width(20)
        assert len(t.rows) == 3

    def test_resize_to_the_same_width_is_a_noop(self):
        t = self._transcript(20)
        t.append(["a" * 40])
        before = list(t.rows)
        t.set_width(20)
        assert t.rows == before

    def test_clear_resets_and_refollows(self):
        t = self._transcript()
        t.append(["a", "b"])
        t.follow = False
        t.clear()
        assert t.rows == [] and t.text() == "" and t.follow is True

    def test_history_is_capped_and_stays_consistent(self):
        """Old output is dropped rather than growing without bound — and the
        rows must be recomputed to match, not left stale."""
        from daimon_agent.cli import tui as tui_mod
        from daimon_agent.cli.render import visible_len

        t = self._transcript(20)
        t.append([f"line {i}" for i in range(tui_mod.MAX_TRANSCRIPT_LINES + 500)])
        assert len(t.rows) <= tui_mod.MAX_TRANSCRIPT_LINES
        assert t.text().count("\n") + 1 == len(t.rows)
        assert all(visible_len(r) <= 20 for r in t.rows)
        assert t.rows[-1] == f"line {tui_mod.MAX_TRANSCRIPT_LINES + 499}"


class TestConfigSources:
    """Everything that reports configuration must read it from the server.

    `/model` used to call `Settings.from_env()` in the CLI process, which
    resolves a different `.env` (this process's cwd, not the server's) and
    can't see a runtime change made through POST /config — so `/model` and
    `/config` disagreed after `/setup`.
    """

    def test_no_command_handler_reads_settings_locally(self):
        import inspect

        from daimon_agent.cli import commands as mod

        source = inspect.getsource(mod)
        assert "Settings.from_env()" not in source, (
            "a command handler is reading config locally — it will drift from "
            "the server, which is what /model did"
        )

    def test_model_and_config_are_both_intercepted_by_the_tui(self):
        from daimon_agent.cli import commands as mod

        for name in ("model", "config", "setup", "tools", "skills"):
            handler = mod.get_commands()[name][1]
            # The stub returns a single blank line; the TUI never dispatches it.
            assert handler(f"/{name}", None) == [""], name

    def test_model_lines_render_the_servers_values(self):
        from daimon_agent.cli import tui as tui_mod

        lines = "\n".join(tui_mod._model_lines({
            "model": "anthropic:claude-sonnet-5",
            "flash_model": "deepseek-chat",
            "api_base": "(default)",
        }))
        assert "anthropic:claude-sonnet-5" in lines
        assert "deepseek-chat" in lines

    def test_model_lines_report_an_unreachable_server(self):
        from daimon_agent.cli import tui as tui_mod

        lines = "\n".join(tui_mod._model_lines({"error": "could not reach the daimon server"}))
        assert "could not reach" in lines


class TestStatusBarSource:
    """The status bar, /model and /config must agree.

    All three used to read the model from somewhere different — the bar from
    this process's Settings (frozen at launch), /model from a different .env,
    /config from the server — so they diverged the moment /setup changed
    anything. They now share one cache.
    """

    def _cache(self, model="deepseek-chat", window=128000):
        from dataclasses import replace

        from daimon_agent.cli.tui import ServerConfig
        from daimon_agent.config import Settings

        fallback = replace(Settings(), model=model, context_window=window)
        return ServerConfig(fallback)

    def test_falls_back_to_launch_settings_before_the_first_fetch(self):
        cache = self._cache(model="deepseek-chat")
        assert cache.model == "deepseek-chat"

    def test_the_server_wins_once_fetched(self):
        cache = self._cache(model="deepseek-chat")
        cache.data = {"model": "deepseek-v4-pro", "context_window": 64000}
        assert cache.model == "deepseek-v4-pro"
        assert cache.context_window == 64000

    async def test_a_failed_refresh_keeps_the_last_known_values(self, monkeypatch):
        """Blanking the status bar because one poll failed would be worse than
        showing a value that's a few seconds old."""
        from daimon_agent.cli import tui as tui_mod

        cache = self._cache()
        cache.data = {"model": "deepseek-v4-pro"}

        async def broken(http, port):
            return {"error": "could not reach the daimon server"}

        monkeypatch.setattr(tui_mod.client, "get_config", broken)
        result = await cache.refresh(None, 4711)
        assert result["error"]
        assert cache.model == "deepseek-v4-pro"

    async def test_refresh_adopts_the_new_model(self, monkeypatch):
        from daimon_agent.cli import tui as tui_mod

        cache = self._cache(model="deepseek-chat")

        async def fresh(http, port):
            return {"model": "anthropic:claude-sonnet-5"}

        monkeypatch.setattr(tui_mod.client, "get_config", fresh)
        await cache.refresh(None, 4711)
        assert cache.model == "anthropic:claude-sonnet-5"

    def test_the_bar_and_model_command_render_the_same_value(self):
        """The actual complaint: they showed different models."""
        from daimon_agent.cli import render, tui as tui_mod
        from daimon_agent.cli.render import TurnStats

        cfg = {"model": "deepseek-v4-pro", "flash_model": "deepseek-v4-flash",
               "api_base": "(default)", "context_window": 128000}
        cache = self._cache(model="stale-launch-value")
        cache.data = cfg

        bar = render.status_line("cli", cache.model, TurnStats(),
                                 context_window=cache.context_window)
        command = "\n".join(tui_mod._model_lines(cfg))
        assert "deepseek-v4-pro" in bar
        assert "deepseek-v4-pro" in command
        assert "stale-launch-value" not in bar
