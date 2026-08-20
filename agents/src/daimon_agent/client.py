"""Thin client for the daimon-agent HTTP server — the whole `daimon` CLI.

The heavy stack (graph, tools, browser, memory) lives in one background
server process. This module knows how to find one, spawn one, talk to one,
and stop one:

- `ensure_server` probes the configured port and adopts whatever answers
  `/health`; otherwise it spawns `python -m daimon_agent.server` with
  `cwd=agents/`, logs to a CLI-owned run dir, and waits up to 30s for it to
  become healthy. The spawned server writes its own pidfile
  (`daimon-agent.pid`, env `DAIMON_PIDFILE`) so the pidfile always names the
  live server — no spawner-side write race.
- `stream_turn` streams one `/task` turn (line-framed NDJSON, terminal event
  = `done`/`error`/`ask`) through a caller-provided `aiohttp.ClientSession`;
  `resume_turn` answers an `ask` and streams the continuation the same way.
- `stop_server` kills only a server that *we* spawned: pidfile present AND
  pid alive AND the recorded port still answers `/health` (never a recycled
  pid). Adopted and app-managed servers are left alone.
- `list_sessions` reads the checkpointer's thread ids read-only for `--list`.

Known limitation (shared state, pre-existing): one server process holds a
single shared browser tab and a single shared REPL, so two sessions that use
the browser or the REPL *simultaneously* interfere. The server's per-session
turn locks serialize each session's turns; only cross-session browser/REPL
use overlaps.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import aiohttp

from .config import Settings
from .events import TERMINAL_TYPES

# The CLI-owned run dir. Deliberately NOT the Tauri app's run dir, whose
# orphan sweep would kill our server on app exit.
def default_run_dir() -> Path:
    return Path.home() / ".local" / "share" / "daimon" / "run"


class ClientError(RuntimeError):
    """Any failure to reach, spawn, or stream from the server."""


def _agents_cwd() -> Path:
    """The cwd the server is spawned with: the agents/ package root when
    this code runs from an (editable) install, else the caller's cwd. The
    server resolves its cwd-relative `.env`/`vault`/`memory` against it."""
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").exists():
        return root
    return Path.cwd()


def server_checkpoints_db(settings: Settings) -> Path:
    """The checkpoints DB the *server* will use — `--list` must read that
    one, not the CLI's cwd-relative default. The server (spawned with
    cwd=agents/) resolves relative paths against agents/, so we re-anchor
    relative paths there; absolute paths (explicit DAIMON_CHECKPOINTS_DB)
    pass through verbatim."""
    db = settings.checkpoints_db
    if db.is_absolute():
        return db
    return _agents_cwd() / db


# --- process management ------------------------------------------------------

def _read_pid(pidfile: Path) -> int | None:
    """First line of the server-written pidfile (pid). None on garbage —
    the server writes it with one atomic write_text, so garbage means a
    crash mid-write and the file is treated as absent."""
    try:
        return int(pidfile.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_port(pidfile: Path) -> int | None:
    try:
        return int(pidfile.read_text(encoding="utf-8").splitlines()[1])
    except (OSError, ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by another user
        return True
    return True


def _kill_group(pid: int) -> None:
    # The server is spawned with start_new_session=True, so its pid is a
    # process-group leader and killpg takes the ipykernel children too.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


async def _health(port: int) -> bool:
    try:
        timeout = aiohttp.ClientTimeout(total=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                return resp.status == 200
    except (aiohttp.ClientError, OSError):
        return False


def _spawn_server(port: int, *, run_dir: Path) -> subprocess.Popen:
    """Start `python -m daimon_agent.server` with cwd=agents/, the port as
    real env (it beats any .env value), and DAIMON_PIDFILE for the server's
    own pidfile. The env is plain os.environ — cwd-`.env` values the CLI's
    Settings merged must NOT leak into the server; agents/.env governs it
    (the server reads it itself with its own cwd)."""
    log_out = run_dir / "daimon-agent.out.log"
    log_err = run_dir / "daimon-agent.err.log"
    run_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PORT": str(port),
        "DAIMON_PIDFILE": str(run_dir / "daimon-agent.pid"),
        # Carry the workspace through so the server uses the CLI's CWD, not its
        # own cwd (agents/).  If DAIMON_WORKSPACE_DIR is already set in the
        # environment (e.g. by the CLI), it flows via **os.environ; this
        # explicit key ensures it's present even when spawning from outside the
        # CLI (tests, direct calls).
        "DAIMON_WORKSPACE_DIR": os.environ.get("DAIMON_WORKSPACE_DIR", os.getcwd()),
    }
    out_f = open(log_out, "ab")
    err_f = open(log_err, "ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "daimon_agent.server"],
            cwd=_agents_cwd(),
            env=env,
            stdout=out_f,
            stderr=err_f,
            start_new_session=True,
        )
    finally:
        out_f.close()
        err_f.close()


def _tail(path: Path, n: int = 2000) -> str:
    try:
        tail = path.read_text(encoding="utf-8", errors="replace")[-n:]
        return f"; log tail: {tail!r}" if tail.strip() else ""
    except OSError:
        return ""


async def _wait_healthy(port: int, proc: subprocess.Popen, *, err_log: Path, timeout: float = 60.0) -> None:
    """Poll /health every 500ms; on timeout or early exit, kill the process
    group and raise with the log tail. 60s covers graph build + vault
    indexing + PinchTab auto-start (setup-pinchtab.sh has its own 60s
    timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _health(port):
            return
        if proc.poll() is not None:
            raise ClientError(
                f"server exited early (code {proc.returncode}){_tail(err_log)}"
            )
        await asyncio.sleep(0.5)
    _kill_group(proc.pid)
    raise ClientError(f"server did not become healthy within {timeout:.0f}s{_tail(err_log)}")


def _sweep_stale(run_dir: Path) -> None:
    """The port probe failed, so any pidfile here is stale (the server writes
    its own pidfile — a live one would have answered /health). Kill the
    recorded process group if it still exists and remove the file."""
    pidfile = run_dir / "daimon-agent.pid"
    pid = _read_pid(pidfile) if pidfile.exists() else None
    if pid is not None and _pid_alive(pid):
        _kill_group(pid)
    with contextlib.suppress(OSError):
        pidfile.unlink()


async def _get_server_workspace(port: int) -> str | None:
    """Return the resolved workspace of a running server from its /health
    payload, or None when unreachable or the server predates the workspace
    field (pre-0.1)."""
    try:
        timeout = aiohttp.ClientTimeout(total=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("workspace")
    except (aiohttp.ClientError, OSError):
        pass
    return None


async def ensure_server(settings: Settings, *, run_dir: Path | None = None) -> tuple[int, bool]:
    """Make sure a daimon server answers on settings.port: adopt one that
    does, spawn one otherwise. Returns (port, spawned). Mirrors the Tauri
    app's ensure in app/src-tauri/src/agent.rs — the CLI and the app can
    share a server on 4711, and each kills only its own.

    When adopting an existing server, verifies that its workspace matches
    the configured one.  A mismatch (or an old server that doesn't report
    its workspace) means the server was started from a different directory —
    kill it and spawn a fresh one with the correct workspace."""
    run_dir = run_dir or default_run_dir()
    port = settings.port
    expected_ws = str(settings.resolved_workspace_dir.resolve())

    if await _health(port):
        # Adopting — check workspace compatibility.
        actual_ws = await _get_server_workspace(port)
        if actual_ws is None:
            # Old server that doesn't report workspace — can't verify, so
            # restart to guarantee the correct workspace is used.
            print(
                f"[daimon] running server does not report its workspace "
                f"(pre-0.1) — restarting to use {expected_ws}",
                file=sys.stderr,
            )
        elif actual_ws != expected_ws:
            print(
                f"[daimon] server workspace mismatch — "
                f"expected {expected_ws}, got {actual_ws} — restarting",
                file=sys.stderr,
            )
        else:
            return port, False
        # Mismatch or unknown — kill the old server before spawning.
        _sweep_stale(run_dir)

    # No healthy server with the right workspace — spawn a fresh one.
    _sweep_stale(run_dir)  # no-op when the pidfile is already gone
    # Brief yield so the OS can release the port after SIGKILL.
    await asyncio.sleep(0.5)
    proc = _spawn_server(port, run_dir=run_dir)
    await _wait_healthy(port, proc, err_log=run_dir / "daimon-agent.err.log")
    return port, True


async def stop_server(run_dir: Path | None = None) -> tuple[bool, str]:
    """Stop a CLI-spawned server, and only that: pidfile present AND pid
    alive AND the recorded port still answers /health. Anything else is
    refused (a recycled pid must never be killed). Returns (stopped,
    message)."""
    run_dir = run_dir or default_run_dir()
    pidfile = run_dir / "daimon-agent.pid"
    if not pidfile.exists():
        return False, "no daimon server to stop (no pidfile)"
    pid = _read_pid(pidfile)
    if pid is None:
        return False, "pidfile is corrupt; nothing stopped"
    if not _pid_alive(pid):
        with contextlib.suppress(OSError):
            pidfile.unlink()
        return False, "the recorded server is already dead; stale pidfile removed"
    port = _read_port(pidfile)
    if port is not None and await _health(port):
        # Healthy server — clean shutdown.
        _kill_group(pid)
        with contextlib.suppress(OSError):
            pidfile.unlink()
        return True, f"stopped daimon server (pid {pid}, port {port})"

    # The PID is alive but the server isn't answering on its recorded port.
    # Most likely a hung/stuck server (e.g. PinchTab startup wedged).
    # A recycled PID is theoretically possible but vanishingly unlikely
    # for a PID we ourselves recorded.  Kill it so the user can recover.
    _kill_group(pid)
    with contextlib.suppress(OSError):
        pidfile.unlink()
    return True, (
        f"killed hung daimon server (pid {pid}) — "
        f"it was alive but not responding on port {port or '?'}"
    )


# --- the task stream ---------------------------------------------------------

async def _stream_ndjson(
    http: aiohttp.ClientSession,
    url: str,
    body: dict,
    emit: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """POST `body` and stream the NDJSON response to `emit`, returning the
    terminal event. Shared by `stream_turn` and `resume_turn` — they differ
    only in endpoint and payload."""
    try:
        async with http.post(url, json=body) as resp:
            if resp.status != 200:
                raise ClientError(f"server rejected the task (HTTP {resp.status})")
            terminal_event: dict[str, Any] | None = None
            # Frame the stream ourselves: resp.content yields raw chunks that
            # can split an event mid-line (and the server writes one event
            # per \n-terminated line), so buffer partial lines between reads.
            buf = b""
            async for raw in resp.content:
                buf += raw
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = buf[:nl].strip()
                    buf = buf[nl + 1 :]
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ClientError(f"bad event from server: {line[:120]!r}") from exc
                    emit(event)
                    if event.get("type") in TERMINAL_TYPES:
                        terminal_event = event
                        break
                if terminal_event is not None:
                    break
            if terminal_event is None:
                # Stream ended without a terminal event (e.g. the server
                # died mid-turn and the connection just EOF'd).
                raise ClientError("stream ended without a done/error event")
            return terminal_event
    except aiohttp.ClientError as exc:
        raise ClientError(f"could not reach the daimon server: {exc}") from exc


async def stream_turn(
    http: aiohttp.ClientSession,
    port: int,
    session_id: str,
    instruction: str,
    emit: Callable[[dict[str, Any]], None],
    agent: str | None = None,  # deprecated — single unified agent; kept for backward compat
    *,
    capabilities: list[str] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """POST one `/task` and stream the NDJSON events to `emit`.

    Returns the terminal event dict — `done` (with `result`), `error` (with
    `message`), or `ask` (the turn suspended awaiting an answer; reply with
    `resume_turn`). Use `turn_result()` when all you want is the text.

    Raises ClientError on transport failure, HTTP rejection, malformed events,
    or a stream that ends without a terminal event. The caller owns `http` (the
    CLI keeps one long-lived session for the whole loop; tests pass
    TestClient.session).

    `capabilities` advertises what this client can render — passing "ask" is
    what makes the server offer the agent its ask_user/present_plan tools, so a
    client that can't answer a question never gets asked one.
    """
    body: dict = {"instruction": instruction, "session_id": session_id}
    if agent:
        body["agent"] = agent  # backward compat — server ignores it
    if capabilities:
        body["capabilities"] = capabilities
    if mode:
        body["mode"] = mode
    return await _stream_ndjson(http, f"http://127.0.0.1:{port}/task", body, emit)


async def resume_turn(
    http: aiohttp.ClientSession,
    port: int,
    session_id: str,
    ask_id: str,
    answer: Any,
    emit: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Answer a suspended `ask` and stream the rest of the turn. Same return
    contract as `stream_turn` — the continuation can itself end in another
    `ask`, so callers loop until they see `done` or `error`."""
    body = {"session_id": session_id, "ask_id": ask_id, "answer": answer}
    return await _stream_ndjson(http, f"http://127.0.0.1:{port}/resume", body, emit)


def turn_result(terminal: dict[str, Any]) -> str | None:
    """The result text from a terminal event, or None for error/ask. Keeps the
    old `stream_turn` return shape available to callers that only want text."""
    return terminal.get("result") if terminal.get("type") == "done" else None


# --- introspection -----------------------------------------------------------

async def list_tools(
    http: aiohttp.ClientSession, port: int, agent: str | None = None
) -> list[dict[str, str]]:
    """GET /tools — return the tool list for the unified agent.
    Returns a list of dicts with `name` and `description` keys.
    The `agent` parameter is kept for backward compat but ignored."""
    try:
        params: dict = {}
        if agent:
            params["agent"] = agent  # backward compat — server ignores it
        async with http.get(
            f"http://127.0.0.1:{port}/tools", params=params
        ) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except (aiohttp.ClientError, OSError):
        return []

def list_sessions(db_path: Path) -> list[str]:
    """Distinct thread ids from the checkpointer DB, sorted. Read-only URI
    connection — never creates the file; [] on a missing DB or table."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
        ).fetchall()
        return [row[0] for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
