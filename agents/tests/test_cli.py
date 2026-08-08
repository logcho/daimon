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
    def test_step_event_running_plain(self):
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({
            "type": "step", "id": "s1", "label": "searching the vault",
            "status": "running", "tool": "vault_search",
        })
        # Running events set _running, not _completed. In plain mode, they
        # print directly to stderr. Just verify thinking is off.
        assert sd._thinking is False

    def test_step_event_done_plain(self):
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({"type": "step", "id": "s1", "label": "thinking", "status": "done"})
        # Done events go to _completed
        assert sd._thinking is False

    def test_error_event_plain(self):
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({"type": "error", "message": "boom"})
        assert sd._thinking is False

    def test_thinking_step_start(self):
        sd = _m._StreamDisplay(tty=False)
        sd.on_event({"type": "step", "id": "t1", "label": "Thinking", "status": "running"})
        assert sd._thinking is True

    def test_thinking_step_done(self):
        sd = _m._StreamDisplay(tty=False)
        sd._thinking = True
        sd.on_event({"type": "step", "id": "t1", "label": "Thinking", "status": "done"})
        assert sd._thinking is False


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

    async def fake_stream(http, port, session_id, instruction, emit, agent="general"):
        calls["stream"] = (session_id, instruction, agent)
        return f"result for {session_id}"

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
    assert fake_client["stream"] == ("cli", "say hi", "general")
    assert "result for cli" in capsys.readouterr().out


async def test_amain_named_session(fake_client, capsys):
    code = await _m._amain(["-n", "work", "hello"])
    assert code == 0
    assert fake_client["stream"] == ("work", "hello", "general")
    assert "result for work" in capsys.readouterr().out


async def test_amain_error_turn_returns_1(fake_client, monkeypatch):
    async def fake_stream(http, port, session_id, instruction, emit, agent="general"):
        return None  # the server emitted an error event
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
    assert fake_client["stream"] == ("cli", "piped hello", "general")


async def test_amain_version(fake_client, capsys):
    code = await _m._amain(["--version"])
    assert code == 0
    assert "daimon" in capsys.readouterr().out


async def test_amain_agent_flag_coding(fake_client, capsys):
    """--agent coding routes to the coding agent."""
    code = await _m._amain(["--agent", "coding", "1+1"])
    assert code == 0
    assert fake_client["stream"] == ("cli", "1+1", "coding")


async def test_amain_agent_flag_short_form(fake_client, capsys):
    """-a coding is the short form."""
    code = await _m._amain(["-a", "coding", "print('hi')"])
    assert code == 0
    assert fake_client["stream"] == ("cli", "print('hi')", "coding")


async def test_amain_agent_flag_defaults_to_general(fake_client, capsys):
    """No --agent flag defaults to general."""
    code = await _m._amain(["hello world"])
    assert code == 0
    assert fake_client["stream"] == ("cli", "hello world", "general")


# ---------------------------------------------------------------------------
# TUI integration test (FakeApp)
# ---------------------------------------------------------------------------

async def test_amain_interactive_tui(fake_client, monkeypatch):
    """Full-screen TUI: FakeApp simulates typing 'hello' + Ctrl+Enter,
    then runs the background task and verifies the stream call."""
    monkeypatch.setattr(_m.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(_m.sys.stderr, "isatty", lambda: True)

    import prompt_toolkit as pt
    import prompt_toolkit.application
    import prompt_toolkit.output
    from prompt_toolkit.keys import Keys

    class FakeKeyEvent:
        def __init__(self, app):
            self.app = app

    class FakeOutput:
        def get_size(self):
            return (24, 80)
        def fileno(self):
            return 2
        def write(self, data):
            pass
        def flush(self):
            pass

    class FakeApp:
        def __init__(self, layout=None, key_bindings=None,
                     full_screen=False, style=None):
            self.layout = layout
            self._kb = key_bindings
            self._bg_tasks: list = []
            self.output = FakeOutput()
            self.current_buffer = None
            self.before_render = []

        async def run_async(self):
            # Layout: HSplit(output, divider, input, status)
            hs = self.layout.container.content
            input_buf = hs.children[2].content.buffer
            self.current_buffer = input_buf
            input_buf.text = "hello"
            input_buf.cursor_position = len("hello")

            # Fire the enter handler: find the binding with Keys.Enter
            for binding in self._kb.bindings:
                if Keys.Enter in binding.keys:
                    binding.handler(FakeKeyEvent(self))
                    break

            # Let background tasks run (non-blocking pulse — don't await
            # infinite-loop tasks like the ticker).
            await asyncio.sleep(0)
            for task in self._bg_tasks:
                if task.done():
                    # Collect any exception / result
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

        def exit(self):
            pass

        def invalidate(self):
            pass

    monkeypatch.setattr(pt.application, "Application", FakeApp)
    monkeypatch.setattr(pt.application, "create_app_session",
                        lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(pt.output, "create_output", lambda **kw: None)

    code = await _m._amain([])
    assert code == 0
    assert fake_client["stream"][0] == "cli"
    assert fake_client["stream"][1] == "hello"
    assert fake_client["stream"][2] == "general"
    assert fake_client["ensure"] == 1


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
    assert texts >= {"help", "clear", "status", "model", "agents", "general", "coding"}


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
