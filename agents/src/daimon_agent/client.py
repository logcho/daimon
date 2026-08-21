"""Thin client for the daimon-agent HTTP server — the whole `daimon` CLI.

The heavy stack (graph, tools, browser, memory) lives in a background server
process — one per *workspace*, so two different project directories never
share (or fight over) the same process. This module knows how to find one,
spawn one, talk to one, and stop one:

- `ensure_server` resolves the current workspace's own run dir
  (`workspace_run_dir`) and adopts a live, healthy server already recorded
  there; otherwise it allocates a fresh port and spawns
  `python -m daimon_agent.server` with `cwd=agents/`, logs to that
  workspace's run dir, and waits up to 60s for it to become healthy. The
  spawned server writes its own pidfile (`daimon-agent.pid`, env
  `DAIMON_PIDFILE`) so the pidfile always names the live server — no
  spawner-side write race. Because each workspace has its own run dir, two
  different directories can never be mistaken for one another — there is no
  "workspace mismatch" to detect or recover from any more.
- `stream_turn` streams one `/task` turn (line-framed NDJSON, terminal event
  = `done`/`error`/`ask`) through a caller-provided `aiohttp.ClientSession`;
  `resume_turn` answers an `ask` and streams the continuation the same way.
- `stop_server` kills only a server that *we* spawned for the current
  workspace: pidfile present AND pid alive AND the recorded port still
  answers `/health` (never a recycled pid). Other workspaces' servers and
  app-managed servers are left alone.
- `list_sessions` reads the checkpointer's thread ids read-only for `--list`.

Known limitation (shared state, pre-existing, now scoped to one workspace
instead of the whole machine): one server process holds a single shared
browser tab and a single shared REPL, so two *named sessions in the same
workspace* that use the browser or the REPL simultaneously interfere. The
server's per-session turn locks serialize each session's turns; only
cross-session browser/REPL use overlaps.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import aiohttp

from .config import Settings
from .events import TERMINAL_TYPES


def default_run_root() -> Path:
    """Root of every per-workspace run dir (pidfile + logs + sidecar).
    Deliberately NOT the Tauri app's run dir, whose orphan sweep would kill
    our server on app exit."""
    return Path.home() / ".local" / "share" / "daimon" / "run"


def _workspace_key(resolved_workspace: Path) -> str:
    """Stable, filesystem-safe id for a resolved workspace path. A hash
    (not the path itself) survives spaces/unicode/length limits; collisions
    are a non-issue at this key space (64 bits)."""
    return hashlib.sha256(str(resolved_workspace).encode("utf-8")).hexdigest()[:16]


def workspace_run_dir(resolved_workspace: Path, *, root: Path | None = None) -> Path:
    """The run dir for one workspace: `<root>/<hash>/`. Writes a
    `workspace.txt` sidecar recording the literal resolved path, purely so
    the run root stays `ls`-enumerable — `--stop`/`--list`/`--doctor` stay
    scoped to the current workspace (v1), but a future "list every running
    workspace server" can read these sidecars without a layout redesign."""
    root = root or default_run_root()
    d = root / _workspace_key(resolved_workspace)
    d.mkdir(parents=True, exist_ok=True)
    sidecar = d / "workspace.txt"
    text = f"{resolved_workspace}\n"
    if not sidecar.exists() or sidecar.read_text(encoding="utf-8") != text:
        with contextlib.suppress(OSError):
            sidecar.write_text(text, encoding="utf-8")
    return d


def _explicit_port() -> int | None:
    """A user-set PORT env var, honored as a manual override even though the
    default path now allocates a fresh port per workspace instead of
    hardcoding one well-known port (multiple concurrent workspace servers
    can't all bind the same port)."""
    raw = os.environ.get("PORT")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def allocate_port() -> int:
    """Bind-then-release port allocation — mirrors the Tauri app's
    `allocate_port()` in app/src-tauri/src/agent.rs. A tiny race (something
    else could grab the port before the child binds) that's acceptable for a
    single-machine dev tool."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


async def ensure_server(settings: Settings, *, run_dir: Path | None = None) -> tuple[int, bool]:
    """Make sure a daimon server is running for this workspace: adopt this
    workspace's own server if its pidfile names a live, healthy process;
    spawn a fresh one (on a freshly allocated port, unless PORT is set
    explicitly) otherwise. Returns (port, spawned).

    Unlike the old fixed-port model, two different workspaces get two
    independent run dirs (`workspace_run_dir`), so this can never adopt or
    kill a server that belongs to a different directory — no workspace
    comparison is needed any more."""
    resolved_ws = settings.resolved_workspace_dir.resolve()
    run_dir = run_dir or workspace_run_dir(resolved_ws)
    pidfile = run_dir / "daimon-agent.pid"

    if pidfile.exists():
        pid = _read_pid(pidfile)
        port = _read_port(pidfile)
        if pid is not None and port is not None and _pid_alive(pid) and await _health(port):
            return port, False
        # Dead or unhealthy — sweep before spawning fresh.
        _sweep_stale(run_dir)

    port = _explicit_port() or allocate_port()
    proc = _spawn_server(port, run_dir=run_dir)
    await _wait_healthy(port, proc, err_log=run_dir / "daimon-agent.err.log")
    return port, True


async def find_server(settings: Settings, *, run_dir: Path | None = None) -> int | None:
    """The port of this workspace's running server, or None if there isn't one.

    The adopt half of `ensure_server` with none of the spawning, for work only
    worth doing when a server already exists — clearing a session's in-memory
    state, say, where starting a server just to tell it to forget nothing would
    be absurd. Leaves a stale pidfile alone; whoever spawns next sweeps it."""
    run_dir = run_dir or workspace_run_dir(settings.resolved_workspace_dir.resolve())
    pidfile = run_dir / "daimon-agent.pid"
    if not pidfile.exists():
        return None
    pid, port = _read_pid(pidfile), _read_port(pidfile)
    if pid is None or port is None or not _pid_alive(pid) or not await _health(port):
        return None
    return port


async def stop_server(settings: Settings | None = None, *, run_dir: Path | None = None) -> tuple[bool, str]:
    """Stop the CLI-spawned server for this workspace, and only that:
    pidfile present AND pid alive AND the recorded port still answers
    /health. Anything else is refused (a recycled pid must never be
    killed), and other workspaces' servers are never touched. Returns
    (stopped, message)."""
    if run_dir is None:
        settings = settings or Settings.from_env()
        run_dir = workspace_run_dir(settings.resolved_workspace_dir.resolve())
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

async def list_skills(
    http: aiohttp.ClientSession, port: int
) -> list[dict[str, str]]:
    """GET /skills — the merged vault + project library. Empty on any failure;
    a missing library is not an error worth interrupting the user for."""
    try:
        async with http.get(f"http://127.0.0.1:{port}/skills") as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except (aiohttp.ClientError, OSError):
        return []


async def read_skill(
    http: aiohttp.ClientSession, port: int, name: str
) -> dict[str, str] | None:
    """GET /skills/{name} — one skill with its full content, or None."""
    try:
        async with http.get(f"http://127.0.0.1:{port}/skills/{name}") as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except (aiohttp.ClientError, OSError):
        return None


async def _get_json(http: aiohttp.ClientSession, url: str) -> Any:
    """GET returning parsed JSON, or a `{"error": ...}` dict. The registry
    reaches out to the network, so 'it didn't work and here's why' has to
    survive back to the user rather than becoming an empty list."""
    try:
        async with http.get(url) as resp:
            body = await resp.json()
            if resp.status != 200:
                message = body.get("error") if isinstance(body, dict) else None
                return {"error": message or f"HTTP {resp.status}"}
            return body
    except (aiohttp.ClientError, OSError, ValueError) as exc:
        return {"error": str(exc)}


async def list_models(http: aiohttp.ClientSession, port: int) -> Any:
    """GET /models — every provider's models plus its installed/key state."""
    return await _get_json(http, f"http://127.0.0.1:{port}/models")


async def get_config(http: aiohttp.ClientSession, port: int) -> Any:
    return await _get_json(http, f"http://127.0.0.1:{port}/config")


async def update_config(http: aiohttp.ClientSession, port: int, fields: dict) -> Any:
    """POST /config. Returns `{"error": …}` rather than raising, because every
    caller here wants to show the reason rather than crash — the server's
    rejections ('no anthropic API key') are the useful part."""
    try:
        async with http.post(f"http://127.0.0.1:{port}/config", json=fields) as resp:
            if resp.status == 200:
                return await resp.json()
            try:
                body = await resp.json()
                message = body.get("error") if isinstance(body, dict) else None
            except Exception:
                message = await resp.text()
            return {"error": message or f"HTTP {resp.status}"}
    except (aiohttp.ClientError, OSError, ValueError) as exc:
        return {"error": str(exc)}


async def list_notes(http: aiohttp.ClientSession, port: int) -> Any:
    """GET /vault — the agent's notes, from the server that owns the vault."""
    return await _get_json(http, f"http://127.0.0.1:{port}/vault")


async def read_note(http: aiohttp.ClientSession, port: int, name: str) -> Any:
    return await _get_json(http, f"http://127.0.0.1:{port}/vault/{quote(name)}")


async def _delete(http: aiohttp.ClientSession, url: str) -> Any:
    try:
        async with http.delete(url) as resp:
            if resp.status == 200:
                return await resp.json()
            try:
                body = await resp.json()
                message = body.get("error") if isinstance(body, dict) else None
            except Exception:
                message = await resp.text()
            return {"error": message or f"HTTP {resp.status}"}
    except (aiohttp.ClientError, OSError, ValueError) as exc:
        return {"error": str(exc)}


async def delete_note(http: aiohttp.ClientSession, port: int, name: str) -> Any:
    return await _delete(http, f"http://127.0.0.1:{port}/vault/{quote(name)}")


async def delete_skill(http: aiohttp.ClientSession, port: int, name: str) -> Any:
    return await _delete(http, f"http://127.0.0.1:{port}/skills/{quote(name)}")


async def clear_session(http: aiohttp.ClientSession, port: int, session_id: str) -> Any:
    """DELETE /sessions/{id} — the server forgets this conversation: its
    stored history, and the todo list, read registry and REPL kernel keyed to
    the same id. 409 while a turn is running."""
    return await _delete(http, f"http://127.0.0.1:{port}/sessions/{quote(session_id)}")


async def registry_search(
    http: aiohttp.ClientSession, port: int, query: str, limit: int = 12
) -> Any:
    return await _get_json(
        http, f"http://127.0.0.1:{port}/registry/search?q={quote(query)}&limit={limit}"
    )


async def registry_item(http: aiohttp.ClientSession, port: int, slug: str) -> Any:
    return await _get_json(http, f"http://127.0.0.1:{port}/registry/item/{quote(slug)}")


async def registry_preview(
    http: aiohttp.ClientSession, port: int, slug: str, path: str = ""
) -> Any:
    """The bundle manifest, fetched but not written — what the user reviews."""
    url = f"http://127.0.0.1:{port}/registry/preview/{quote(slug)}"
    if path:
        url += f"?path={quote(path)}"
    return await _get_json(http, url)


async def install_skill(
    http: aiohttp.ClientSession, port: int, slug: str, path: str, scope: str = "vault"
) -> Any:
    try:
        async with http.post(
            f"http://127.0.0.1:{port}/skills/install",
            json={"slug": slug, "path": path, "scope": scope},
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                message = body.get("error") if isinstance(body, dict) else None
                return {"error": message or f"HTTP {resp.status}"}
            return body
    except (aiohttp.ClientError, OSError, ValueError) as exc:
        return {"error": str(exc)}


def forget_session_history(db_path: Path, session_id: str) -> bool:
    """Delete one thread's checkpoints straight from the DB — the no-server
    path for the same job `clear_session` asks the server to do. The history
    outlives the server process, so "nothing is running" is not the same as
    "nothing to forget". Clears both tables `AsyncSqliteSaver.adelete_thread`
    does. Returns False only when the delete genuinely failed; a missing file
    or table is nothing stored yet, which is the asked-for state already."""
    if not db_path.exists():
        return True
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return False
    try:
        # The server (app or CLI) may hold the write lock — wait, as the
        # checkpointer itself does, rather than failing the clear.
        conn.execute("PRAGMA busy_timeout = 10000")
        for table in ("checkpoints", "writes"):
            with contextlib.suppress(sqlite3.OperationalError):  # table not created yet
                conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (session_id,))
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


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
