"""Real PTYs running real shells. Nothing here is mocked: the behaviour worth
testing (a controlling terminal, EOF on exit, a shared screen) only exists at
the OS level."""

from __future__ import annotations

import asyncio
import os

import pytest

from daimon_agent.terminals import KITTY_QUERY, KITTY_REPLY, RING_BYTES, TerminalManager


class Recorder:
    """A client: collects output and notices the shell exiting."""

    def __init__(self) -> None:
        self.out = bytearray()
        self.exit_code: int | None = None
        self.exited = asyncio.Event()

    def __call__(self, kind: str, _term_id: str, payload) -> None:
        if kind == "output":
            self.out += payload
        else:
            self.exit_code = payload
            self.exited.set()

    async def wait_for(self, needle: bytes, timeout: float = 10.0) -> None:
        async def poll() -> None:
            while needle not in bytes(self.out):
                await asyncio.sleep(0.02)

        await asyncio.wait_for(poll(), timeout=timeout)


async def ready(term, watcher: Recorder, timeout: float = 15.0) -> None:
    """Wait until the shell is actually reading input.

    A login shell reads .zprofile, prints a banner and starts its line editor
    asynchronously, and bytes written before that can be swallowed. Probing in
    a retry loop is the only reliable signal — there is no "shell is ready"
    event to wait on.
    """

    async def probe() -> None:
        while True:
            term.write(b"printf 'DAIMON-%s\\n' READY\n")
            try:
                await watcher.wait_for(b"DAIMON-READY", timeout=1.0)
                return
            except asyncio.TimeoutError:
                continue

    await asyncio.wait_for(probe(), timeout=timeout)


@pytest.fixture
async def manager():
    mgr = TerminalManager()
    yield mgr
    mgr.close_all()


async def test_a_terminal_runs_a_shell_and_streams_its_output(manager) -> None:
    term = await manager.open(cols=100, rows=40)
    watcher = Recorder()
    term.attach(watcher, cols=100, rows=40)

    term.write(b"echo hello-from-daimon\n")
    await watcher.wait_for(b"hello-from-daimon")

    assert term.pid and term.pid > 0
    assert term.exited is False


async def test_the_shell_gets_a_controlling_terminal(manager) -> None:
    """Without one there is no foreground process group: no job control, and
    Ctrl-C never becomes SIGINT. `tty` prints the device only if there is one."""
    term = await manager.open()
    watcher = Recorder()
    term.attach(watcher)

    term.write(b"tty\n")
    await watcher.wait_for(b"/dev/")
    assert b"not a tty" not in bytes(watcher.out)


async def test_ctrl_c_interrupts_the_foreground_program(manager) -> None:
    """The end-to-end proof that job control works: without a controlling
    terminal, Ctrl-C is just a byte and `sleep` runs to completion."""
    term = await manager.open()
    watcher = Recorder()
    term.attach(watcher)
    await ready(term, watcher)

    term.write(b"sleep 30\n")
    await asyncio.sleep(0.5)
    term.write(b"\x03")  # Ctrl-C
    # The shell taking a new command is the signal. Not "the rest of the line
    # runs" — Ctrl-C abandons the whole line — and not the marker appearing at
    # all, since the tty echoes whatever was typed.
    term.write(b"printf 'AFTER-%s\\n' INT\n")
    await watcher.wait_for(b"AFTER-INT", timeout=8)


async def test_the_terminal_starts_in_the_requested_directory(manager, tmp_path) -> None:
    marker = tmp_path / "somewhere"
    marker.mkdir()
    term = await manager.open(cwd=str(marker))
    watcher = Recorder()
    term.attach(watcher)

    term.write(b"pwd\n")
    await watcher.wait_for(marker.name.encode())


async def test_a_nonexistent_cwd_falls_back_to_home_rather_than_failing(manager) -> None:
    term = await manager.open(cwd="/definitely/not/a/real/path")
    assert term.cwd == os.path.expanduser("~")


async def test_input_is_written_verbatim_with_no_newline_added(manager) -> None:
    """A command the agent staged has to land on the input line *unexecuted*
    so the user can read it before pressing Enter."""
    term = await manager.open()
    watcher = Recorder()
    term.attach(watcher)
    await asyncio.sleep(0.5)  # let the prompt settle

    term.write(b"echo not-run-yet")
    await watcher.wait_for(b"echo not-run-yet")  # echoed by the tty
    await asyncio.sleep(0.3)
    assert b"not-run-yet\r\n" not in bytes(watcher.out).split(b"echo not-run-yet")[-1]


async def test_the_shell_exiting_is_reported_with_its_code(manager) -> None:
    term = await manager.open()
    watcher = Recorder()
    term.attach(watcher)

    term.write(b"exit 3\n")
    await asyncio.wait_for(watcher.exited.wait(), timeout=10)
    assert term.exited is True
    assert watcher.exit_code == 3


async def test_a_killed_shell_reports_no_exit_code_rather_than_a_fake_one(manager) -> None:
    """A signal death is not exit status 1, and saying so would be a lie the
    UI would render as a failed command."""
    term = await manager.open()
    watcher = Recorder()
    term.attach(watcher)

    manager.close(term.id)
    await asyncio.wait_for(watcher.exited.wait(), timeout=10)
    assert watcher.exit_code is None


async def test_reattaching_replays_the_scrollback(manager) -> None:
    """The whole reason the buffer exists: the Rust version streamed straight
    through, so a client arriving later saw an empty screen."""
    term = await manager.open()
    first = Recorder()
    term.attach(first)
    # printf, so the marker appears only in the *output* — `echo remember-this`
    # would also match the tty's echo of the typed command and pass before the
    # shell had run anything.
    term.write(b"printf 'REMEMBER-%s\\n' THIS\n")
    await first.wait_for(b"REMEMBER-THIS")
    term.detach(first)

    later = Recorder()
    replay = term.attach(later)
    assert b"REMEMBER-THIS" in replay
    assert replay.startswith(b"\x1bc")  # a reset, so a partial escape can't corrupt


async def test_the_scrollback_is_bounded(manager) -> None:
    term = await manager.open()
    watcher = Recorder()
    term.attach(watcher)
    term.write(b"head -c 400000 /dev/zero | tr '\\0' 'x'\n")

    async def poll() -> None:
        while len(term._ring) < RING_BYTES:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout=20)
    await asyncio.sleep(0.5)
    assert len(term._ring) <= RING_BYTES


async def test_two_clients_watch_the_same_shell(manager) -> None:
    term = await manager.open()
    a, b = Recorder(), Recorder()
    term.attach(a)
    term.attach(b)

    term.write(b"echo shared-screen\n")
    await a.wait_for(b"shared-screen")
    await b.wait_for(b"shared-screen")


async def test_detaching_one_client_leaves_the_other_streaming(manager) -> None:
    term = await manager.open()
    staying, leaving = Recorder(), Recorder()
    term.attach(staying)
    term.attach(leaving)
    term.detach(leaving)

    term.write(b"echo still-here\n")
    await staying.wait_for(b"still-here")
    assert b"still-here" not in bytes(leaving.out)


async def test_the_pty_takes_the_smallest_attached_size(manager) -> None:
    """tmux's rule: a shared screen has one size, and the smallest is the one
    everybody can see all of. A phone attaching must not reflow the desktop."""
    term = await manager.open(cols=200, rows=50)
    desktop, phone = Recorder(), Recorder()
    term.attach(desktop, cols=200, rows=50)
    assert (term.cols, term.rows) == (200, 50)

    term.attach(phone, cols=60, rows=30)
    assert (term.cols, term.rows) == (60, 30)

    # And it goes back when the small client leaves.
    term.detach(phone)
    assert (term.cols, term.rows) == (200, 50)


async def test_the_shell_sees_the_size_it_was_given(manager) -> None:
    term = await manager.open(cols=100, rows=42)
    watcher = Recorder()
    term.attach(watcher, cols=100, rows=42)

    term.write(b"stty size\n")
    await watcher.wait_for(b"42 100")


async def test_resizing_is_ignored_for_a_client_that_is_not_attached(manager) -> None:
    term = await manager.open(cols=90, rows=30)
    attached, stranger = Recorder(), Recorder()
    term.attach(attached, cols=90, rows=30)

    term.resize(stranger, 10, 10)
    assert (term.cols, term.rows) == (90, 30)


async def test_the_kitty_keyboard_query_is_answered_exactly_once(manager) -> None:
    """xterm.js will not answer it, and with several clients attached each of
    them answering would inject duplicate bytes into the shell."""
    term = await manager.open()
    watcher = Recorder()
    term.attach(watcher)
    term.attach(Recorder())  # a second client changes nothing

    sent: list[bytes] = []
    original = term.write
    term.write = lambda data: (sent.append(data), original(data))[1]
    term._absorb(b"prefix" + KITTY_QUERY + b"suffix")

    assert sent == [KITTY_REPLY]


async def test_opening_the_same_id_twice_does_not_spawn_a_second_shell(manager) -> None:
    """The frontend calls open on mount, and a remount must not leak a PTY."""
    first = await manager.open(term_id="tab-1")
    second = await manager.open(term_id="tab-1")
    assert first is second
    assert len(manager.list()) == 1


async def test_terminals_are_listed_with_what_a_client_needs_to_render_them(manager) -> None:
    term = await manager.open(term_id="tab-1", cols=120, rows=40)
    (row,) = manager.list()
    assert row["id"] == "tab-1"
    assert (row["cols"], row["rows"]) == (120, 40)
    assert row["exited"] is False
    assert row["pid"] == term.pid


async def test_closing_a_terminal_that_is_already_gone_is_not_an_error(manager) -> None:
    term = await manager.open()
    assert manager.close(term.id) is True
    assert manager.close(term.id) is False


async def test_closing_hangs_up_the_shells_background_jobs(manager) -> None:
    """A shell that started a server would otherwise leave it orphaned and
    still holding its port.

    Note this cannot be done by killing the shell's process group: job control
    puts each background job in a group of its own, so the shell has to be
    asked to hang them up — and it has to be alive to do it.
    """
    term = await manager.open()
    watcher = Recorder()
    term.attach(watcher)
    import re

    await ready(term, watcher)
    term.write(b"sleep 300 & echo child-pid:$!\n")

    # The tty echoes the typed command, so `child-pid:$!` shows up before the
    # shell has expanded anything — wait for a version with a number in it.
    async def expanded() -> int:
        while True:
            found = re.findall(r"child-pid:(\d+)", bytes(watcher.out).decode(errors="replace"))
            if found:
                return int(found[0])
            await asyncio.sleep(0.02)

    child_pid = await asyncio.wait_for(expanded(), timeout=10)
    manager.close(term.id)

    async def poll() -> None:
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout=10)


async def test_writing_to_an_exited_terminal_is_a_clear_error(manager) -> None:
    term = await manager.open()
    watcher = Recorder()
    term.attach(watcher)
    term.write(b"exit\n")
    await asyncio.wait_for(watcher.exited.wait(), timeout=10)

    with pytest.raises(RuntimeError, match="exited"):
        term.write(b"too late\n")


async def test_live_count_ignores_terminals_whose_shell_has_gone(manager) -> None:
    """It gates idle shutdown: a dead terminal must not keep the server up."""
    term = await manager.open()
    assert manager.live_count == 1

    watcher = Recorder()
    term.attach(watcher)
    term.write(b"exit\n")
    await asyncio.wait_for(watcher.exited.wait(), timeout=10)
    assert manager.live_count == 0
