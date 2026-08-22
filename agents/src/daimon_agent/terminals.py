"""Terminals, owned by the agent server rather than by the desktop app.

Ported from `app/src-tauri/src/terminal.rs`, which ran the user's login shell
in a PTY inside the Tauri process and pushed output over a Tauri event. That
worked exactly as long as the only thing that ever wanted to see a terminal
was the window it was drawn in. A phone cannot reach into the Rust process,
and terminals died with the app.

Moving them here buys three things beyond remote access: a terminal outlives
the app that opened it, several clients can watch one shell at once, and there
is finally a scrollback buffer — the Rust version streamed output straight
through, so the only copy lived in the webview's xterm.js instance and a
reconnecting client got a blank screen.

Like its predecessor, this deliberately runs the user's own login shell on the
host, *outside* the workspace confinement every other execution surface here
respects. That is the point of a terminal and it is why exposing one remotely
is a separate, explicit decision made by the gateway — not something this
module can grant on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import signal
import struct
import sys
import termios
import uuid
from collections.abc import Callable

#: Bytes of raw output kept per terminal for replay. A screen is a few KB; this
#: is a comfortable few hundred lines of scrollback without being a place large
#: output quietly accumulates forever.
RING_BYTES = 256 * 1024

#: One read. Matches the Rust version's buffer.
READ_CHUNK = 4096

DEFAULT_SIZE = (80, 24)

#: How long a shell gets to hang up its own jobs after SIGHUP before the
#: process group is killed outright.
KILL_GRACE_S = 0.5

#: xterm.js does not answer the Kitty keyboard-protocol query, so something has
#: to answer it on its behalf or the querying program waits. The frontend used
#: to do that. It cannot any more: with several clients attached they would each
#: reply and inject duplicate bytes into the shell, so the answer belongs here,
#: where it happens exactly once.
KITTY_QUERY = b"\x1b[?u"
KITTY_REPLY = b"\x1b[?0u"


def _shell() -> str:
    """The user's own shell, the way every other terminal emulator picks it."""
    return os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # TUI programs rely on TERM advertising real capabilities. A server started
    # by launchd or a GUI app has no controlling terminal and may carry no TERM
    # at all, and an unset or `dumb` TERM drops libraries into a degraded
    # non-interactive mode. xterm-256color is simply true here: xterm.js is
    # what is on the other end.
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    # Only when absent, unlike TERM: a user may legitimately want a different
    # UTF-8 locale, but no locale at all garbles the box-drawing characters
    # TUI menus are made of.
    env.setdefault("LANG", "en_US.UTF-8")
    return env


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _take_controlling_terminal() -> None:
    """Run in the child between fork and exec.

    `start_new_session=True` has already called setsid(), and the fd dance has
    already made the pty slave fd 0 — so this claims it as the session's
    controlling terminal. Without it there is no foreground process group,
    which means no job control and Ctrl-C never becomes SIGINT: the shell would
    look right and behave like a pipe.
    """
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass  # already the controlling terminal, or a platform without it


class TerminalSession:
    """One PTY and the shell running in it."""

    def __init__(self, term_id: str, *, cwd: str, cols: int, rows: int) -> None:
        self.id = term_id
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.pid: int | None = None
        self.exited = False
        self.exit_code: int | None = None
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        #: Raw output, capped. Replayed verbatim into a client's emulator,
        #: which is a state machine — feeding it the bytes rebuilds the screen.
        self._ring = bytearray()
        #: Whether the ring has dropped anything off the front. Until it has,
        #: byte 0 is genuinely the start of the stream and must not be trimmed.
        self._ring_truncated = False
        #: listener -> its requested size. The size is per-client because the
        #: PTY has only one, and a phone attaching must not reflow the desktop.
        self._listeners: dict[Callable, tuple[int, int] | None] = {}

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        master_fd, slave_fd = os.openpty()
        _set_winsize(master_fd, self.cols, self.rows)
        try:
            proc = await asyncio.create_subprocess_exec(
                _shell(),
                # A login shell: without it zsh and bash read .zshrc but not
                # .zprofile, and on macOS that is exactly where PATH additions
                # live (Homebrew's shellenv, nvm, ...). A server inherits
                # launchd's bare environment, so skipping this makes tools that
                # work in every other terminal mysteriously absent from this one.
                "-l",
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self.cwd,
                env=_child_env(),
                start_new_session=True,
                preexec_fn=_take_controlling_terminal,
            )
        finally:
            # The slave stays open — and the master's reader never sees EOF —
            # for as long as *any* handle to it lives, this process's own
            # included. Closing it here is what makes the shell exiting
            # observable.
            os.close(slave_fd)

        self._master_fd = master_fd
        self._proc = proc
        self.pid = proc.pid
        os.set_blocking(master_fd, False)
        asyncio.get_running_loop().add_reader(master_fd, self._on_readable)
        asyncio.ensure_future(self._await_exit())

    def _on_readable(self) -> None:
        fd = self._master_fd
        if fd is None:
            return
        try:
            data = os.read(fd, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            # EIO on macOS is how the last slave closing shows up. Not an
            # error — it is the shell having exited.
            data = b""
        if not data:
            self._stop_reading()
            return
        self._absorb(data)

    def _absorb(self, data: bytes) -> None:
        self._ring += data
        if len(self._ring) > RING_BYTES:
            del self._ring[: len(self._ring) - RING_BYTES]
            self._ring_truncated = True
        if KITTY_QUERY in data:
            self.write(KITTY_REPLY)
        for listener in tuple(self._listeners):
            listener("output", self.id, data)

    async def _await_exit(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = await proc.wait()
        # A process killed by a signal reports a negative returncode here.
        # Report that as "no exit code" rather than a number that looks like
        # one — the Rust version made the same distinction for the same reason.
        self.exit_code = code if code is not None and code >= 0 else None
        self.exited = True
        self._stop_reading()
        for listener in tuple(self._listeners):
            listener("exit", self.id, self.exit_code)

    def _stop_reading(self) -> None:
        fd, self._master_fd = self._master_fd, None
        if fd is None:
            return
        try:
            asyncio.get_event_loop().remove_reader(fd)
        except (RuntimeError, ValueError, OSError):
            pass  # loop already closed, or the fd was never registered
        try:
            os.close(fd)
        except OSError:
            pass

    def kill(self) -> None:
        """End the shell and everything it started, the way closing a terminal
        window does.

        Not a plain SIGKILL, and not one process group. Because the shell has a
        controlling terminal it also has *job control*, which puts every job it
        starts — `npm start &`, a `sleep` in the background — into a process
        group of its own. Killing the shell's group therefore misses exactly
        the long-running things worth cleaning up, and SIGKILLing the shell
        first guarantees it: a dead shell cannot hang up its own jobs.

        So: close the pty, which is what makes the kernel treat this as the
        terminal going away, and SIGHUP the shell so it hangs up its jobs the
        way it would if you closed the window. SIGKILL follows as a backstop
        for a shell that ignores it.
        """
        if self.pid is None:
            return
        self._stop_reading()  # closing the master is the hangup the kernel sees
        try:
            pgid = os.getpgid(self.pid)
        except OSError:
            return  # already gone
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGHUP)

        def sigkill() -> None:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)

        try:
            asyncio.get_running_loop().call_later(KILL_GRACE_S, sigkill)
        except RuntimeError:
            sigkill()  # no loop (shutdown): don't wait, just end it

    # --- io -----------------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Raw bytes to the shell. No newline is ever appended — a command the
        agent staged arrives on the input line and the user presses Enter."""
        fd = self._master_fd
        if fd is None:
            raise RuntimeError("terminal has exited")
        while data:
            try:
                written = os.write(fd, data)
            except BlockingIOError:
                return  # the shell is not reading; dropping keystrokes beats blocking
            except OSError as exc:
                raise RuntimeError(f"failed to write to terminal: {exc}") from exc
            data = data[written:]

    # --- clients ------------------------------------------------------------

    def attach(self, listener: Callable, *, cols: int | None = None, rows: int | None = None) -> bytes:
        """Add a client and return what it should render to catch up.

        A full reset leads, so a stray escape sequence cannot corrupt the new
        client's emulator. Only a ring that has actually overflowed gets
        trimmed forward to a line boundary — until then byte 0 really is the
        start of the stream, and dropping its first line would throw away
        output the client has never seen.
        """
        self._listeners[listener] = (cols, rows) if cols and rows else None
        self._apply_size()
        if not self._ring:
            return b""
        tail = bytes(self._ring)
        if self._ring_truncated:
            boundary = tail.find(b"\n")
            if boundary != -1:
                tail = tail[boundary + 1 :]
        return b"\x1bc" + tail

    def detach(self, listener: Callable) -> None:
        self._listeners.pop(listener, None)
        self._apply_size()

    def resize(self, listener: Callable, cols: int, rows: int) -> None:
        if listener in self._listeners:
            self._listeners[listener] = (cols, rows)
        self._apply_size()

    def _apply_size(self) -> None:
        """The PTY gets the smallest attached client's grid.

        tmux's rule, for tmux's reason: a shared screen has one size, and
        picking the smallest means everyone can see all of it. Last-writer-wins
        would let a phone attaching reflow a desktop mid-command.
        """
        sizes = [s for s in self._listeners.values() if s]
        if not sizes:
            return
        cols = min(s[0] for s in sizes)
        rows = min(s[1] for s in sizes)
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        fd = self._master_fd
        if fd is not None:
            try:
                _set_winsize(fd, cols, rows)
            except OSError:
                pass  # the shell exited between the check and the ioctl

    # --- description --------------------------------------------------------

    def describe(self) -> dict:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "cols": self.cols,
            "rows": self.rows,
            "pid": self.pid,
            "exited": self.exited,
            "exit_code": self.exit_code,
            "clients": len(self._listeners),
        }


class TerminalManager:
    """Every terminal this server owns."""

    def __init__(self) -> None:
        self._terminals: dict[str, TerminalSession] = {}

    def get(self, term_id: str) -> TerminalSession | None:
        return self._terminals.get(term_id)

    def list(self) -> list[dict]:
        return [t.describe() for t in self._terminals.values()]

    @property
    def live_count(self) -> int:
        """Terminals still running. A server holding a shell open must not
        idle-exit under whoever is using it."""
        return sum(1 for t in self._terminals.values() if not t.exited)

    async def open(
        self, *, term_id: str | None = None, cwd: str | None = None,
        cols: int = DEFAULT_SIZE[0], rows: int = DEFAULT_SIZE[1],
    ) -> TerminalSession:
        """Start a terminal, or return the existing one for this id.

        Idempotent per id, as the Rust version was: the frontend calls it on
        mount and a remount must not spawn a second shell.
        """
        if term_id and term_id in self._terminals:
            return self._terminals[term_id]
        term_id = term_id or str(uuid.uuid4())
        home = os.path.expanduser("~")
        target = cwd or home
        if not os.path.isdir(target):
            target = home
        term = TerminalSession(term_id, cwd=target, cols=cols, rows=rows)
        self._terminals[term_id] = term
        try:
            await term.start()
        except Exception:
            del self._terminals[term_id]
            raise
        return term

    def close(self, term_id: str) -> bool:
        """End one terminal. Closing something already closed is not a failure."""
        term = self._terminals.pop(term_id, None)
        if term is None:
            return False
        term.kill()
        return True

    def close_all(self) -> None:
        for term_id in list(self._terminals):
            self.close(term_id)
