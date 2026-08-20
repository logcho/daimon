"""The thin client (client.py) in three layers:

1. Framing units against a stub aiohttp session — line framing must survive
   events split mid-line, several events per chunk, and non-ASCII text.
2. Integration through the real server app (TestClient) — a real /task turn
   streamed through stream_turn ends with the scripted result.
3. Process management with monkeypatched health/spawn/wait — adoption vs
   spawn, the stale-pidfile sweep, the kill-only-ours stop matrix, and the
   read-only --list helpers.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from langchain_core.messages import AIMessage

from daimon_agent import client
from daimon_agent.config import Settings
from daimon_agent.server import create_app

from test_server import fake_graph_builder


# --- stubs ------------------------------------------------------------------

class _FakeContent:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()


class _FakeResponse:
    def __init__(self, status: int = 200, chunks: list[bytes] | None = None):
        self.status = status
        self.content = _FakeContent(chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession: records the posted body,
    returns the scripted response, or raises a transport error on demand."""

    def __init__(self, response: _FakeResponse | None = None, raise_err: Exception | None = None):
        self._response = response
        self._raise_err = raise_err
        self.posted: list[dict] = []
        self.urls: list[str] = []

    def post(self, url, json=None):
        if self._raise_err is not None:
            raise self._raise_err
        self.urls.append(url)
        self.posted.append(json)
        return self._response


def _ndjson(*events: dict) -> bytes:
    return b"".join(
        json.dumps(e, ensure_ascii=False).encode("utf-8") + b"\n" for e in events
    )


def _noop_health(result: bool):
    """A scriptable `_health` stub — must be a coroutine, not a plain bool."""
    async def fake(port: int) -> bool:
        return result
    return fake


async def _noop(*args, **kwargs):
    """Anything that must be awaitable but does nothing."""
    return None


# --- layer 1: framing units --------------------------------------------------

async def test_stream_turn_posts_body_and_returns_done_result():
    done = {"type": "done", "result": "The answer is 42."}
    session = _FakeSession(_FakeResponse(chunks=[_ndjson(done)]))
    events: list[dict] = []
    terminal = await client.stream_turn(session, 4711, "cli", "q", events.append)
    assert client.turn_result(terminal) == "The answer is 42."
    assert events == [done]
    assert session.posted == [{"instruction": "q", "session_id": "cli"}]


async def test_stream_turn_advertises_capabilities_and_mode():
    """The server gates the ask tools on the client saying it can answer, so
    what goes in the body decides whether the agent may ask a question."""
    done = {"type": "done", "result": "ok"}
    session = _FakeSession(_FakeResponse(chunks=[_ndjson(done)]))
    await client.stream_turn(
        session, 4711, "cli", "q", lambda e: None, capabilities=["ask"], mode="plan"
    )
    assert session.posted == [
        {"instruction": "q", "session_id": "cli", "capabilities": ["ask"], "mode": "plan"}
    ]


async def test_stream_turn_returns_an_ask_as_terminal():
    """`ask` closes the stream like done/error — the turn is parked in the
    checkpointer waiting for resume_turn, not finished."""
    ask = {
        "type": "ask",
        "id": "a1",
        "kind": "question",
        "question": "Which one?",
        "options": [{"label": "A", "description": ""}],
        "multi_select": False,
    }
    session = _FakeSession(_FakeResponse(chunks=[_ndjson(ask)]))
    terminal = await client.stream_turn(session, 4711, "cli", "q", lambda e: None)
    assert terminal == ask
    assert client.turn_result(terminal) is None


async def test_resume_turn_posts_the_answer():
    done = {"type": "done", "result": "done after answering"}
    session = _FakeSession(_FakeResponse(chunks=[_ndjson(done)]))
    terminal = await client.resume_turn(
        session, 4711, "cli", "a1", "Option A", lambda e: None
    )
    assert client.turn_result(terminal) == "done after answering"
    assert session.urls == ["http://127.0.0.1:4711/resume"]
    assert session.posted == [
        {"session_id": "cli", "ask_id": "a1", "answer": "Option A"}
    ]


async def test_stream_turn_multiple_events_in_one_chunk():
    step = {"type": "step", "id": "s1", "label": "Thinking", "status": "running"}
    done = {"type": "done", "result": "ok"}
    session = _FakeSession(_FakeResponse(chunks=[_ndjson(step, done)]))
    events: list[dict] = []
    terminal = await client.stream_turn(session, 4711, "cli", "q", events.append)
    assert client.turn_result(terminal) == "ok"
    assert events == [step, done]


async def test_stream_turn_event_split_across_chunks():
    running = {"type": "step", "id": "s1", "label": "Thinking", "status": "running"}
    finished = {"type": "step", "id": "s1", "label": "Thinking", "status": "done"}
    done = {"type": "done", "result": "fin"}
    raw = _ndjson(running, finished, done)
    mid = len(raw) // 2
    session = _FakeSession(_FakeResponse(chunks=[raw[:mid], raw[mid:]]))
    events: list[dict] = []
    terminal = await client.stream_turn(session, 4711, "cli", "q", events.append)
    assert client.turn_result(terminal) == "fin"
    assert events == [running, finished, done]


async def test_stream_turn_non_ascii_round_trip():
    done = {"type": "done", "result": "héllo — 中文 ✓"}
    session = _FakeSession(_FakeResponse(chunks=[_ndjson(done)]))
    events: list[dict] = []
    terminal = await client.stream_turn(session, 4711, "cli", "q", events.append)
    assert client.turn_result(terminal) == "héllo — 中文 ✓"


async def test_stream_turn_error_event_returns_the_error():
    err = {"type": "error", "message": "boom"}
    session = _FakeSession(_FakeResponse(chunks=[_ndjson(err)]))
    terminal = await client.stream_turn(session, 4711, "cli", "q", lambda e: None)
    assert terminal == err
    assert client.turn_result(terminal) is None


async def test_stream_turn_truncated_stream_raises():
    # The stream just ends without a done/error — the server died mid-turn.
    step = {"type": "step", "id": "s1", "label": "Thinking", "status": "running"}
    session = _FakeSession(_FakeResponse(chunks=[_ndjson(step)]))
    with pytest.raises(client.ClientError, match="without a done/error"):
        await client.stream_turn(session, 4711, "cli", "q", lambda e: None)


async def test_stream_turn_http_rejection_raises():
    session = _FakeSession(_FakeResponse(status=400))
    with pytest.raises(client.ClientError, match="HTTP 400"):
        await client.stream_turn(session, 4711, "cli", "q", lambda e: None)


async def test_stream_turn_transport_error_raises():
    session = _FakeSession(raise_err=aiohttp.ClientConnectionError("conn refused"))
    with pytest.raises(client.ClientError, match="could not reach"):
        await client.stream_turn(session, 4711, "cli", "q", lambda e: None)


# --- layer 2: integration through the real server ---------------------------

async def test_stream_turn_against_real_server(settings):
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as http:
        app["graph"].router._pro.script = [AIMessage(content="The answer is 42.")]
        events: list[dict] = []
        terminal = await client.stream_turn(
            http.session, http.port, "integ", "What is 2+2?", events.append
        )
        assert client.turn_result(terminal) == "The answer is 42."
        types = [e["type"] for e in events]
        assert types[0] == "step"
        assert types[-1] == "done"


# --- layer 3: process management ---------------------------------------------

class _FakeProc:
    def __init__(self, pid: int = 4242, poll_result: int | None = None):
        self.pid = pid
        self.returncode = poll_result
        self._poll_result = poll_result

    def poll(self) -> int | None:
        return self._poll_result


async def test_ensure_server_adopts_when_healthy(monkeypatch, settings, tmp_path):
    probed: list[int] = []
    async def fake_health(port):
        probed.append(port)
        return True
    monkeypatch.setattr(client, "_health", fake_health)
    # The server reports the same workspace — adoption should succeed.
    async def fake_workspace(port):
        return str(settings.resolved_workspace_dir.resolve())
    monkeypatch.setattr(client, "_get_server_workspace", fake_workspace)
    port, spawned = await client.ensure_server(settings, run_dir=tmp_path)
    assert port == settings.port
    assert spawned is False
    assert probed == [settings.port]


async def test_ensure_server_spawns_when_port_dead(monkeypatch, settings, tmp_path):
    monkeypatch.setattr(client, "_health", _noop_health(False))
    proc = _FakeProc()
    monkeypatch.setattr(client, "_spawn_server", lambda port, run_dir: proc)
    monkeypatch.setattr(client, "_wait_healthy", _noop)
    port, spawned = await client.ensure_server(settings, run_dir=tmp_path)
    assert port == settings.port
    assert spawned is True


async def test_ensure_server_sweeps_stale_pidfile_before_spawn(monkeypatch, settings, tmp_path):
    # A stale pidfile names a dead-by-definition server (the server writes
    # its own pidfile, and this port answered nothing) — kill the recorded
    # group if it still exists, remove the file, then spawn fresh.
    (tmp_path / "daimon-agent.pid").write_text("4242\n4711\n")
    killed: list[int] = []
    monkeypatch.setattr(client, "_health", _noop_health(False))
    monkeypatch.setattr(client, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(client, "_kill_group", lambda pid: killed.append(pid))
    monkeypatch.setattr(client, "_spawn_server", lambda port, run_dir: _FakeProc())
    monkeypatch.setattr(client, "_wait_healthy", _noop)
    port, spawned = await client.ensure_server(settings, run_dir=tmp_path)
    assert spawned is True
    assert killed == [4242]
    assert not (tmp_path / "daimon-agent.pid").exists()


async def test_ensure_server_sweeps_garbage_pidfile(monkeypatch, settings, tmp_path):
    (tmp_path / "daimon-agent.pid").write_text("garbage\n")
    killed: list[int] = []
    monkeypatch.setattr(client, "_health", _noop_health(False))
    monkeypatch.setattr(client, "_kill_group", lambda pid: killed.append(pid))
    monkeypatch.setattr(client, "_spawn_server", lambda port, run_dir: _FakeProc())
    monkeypatch.setattr(client, "_wait_healthy", _noop)
    await client.ensure_server(settings, run_dir=tmp_path)
    assert killed == []
    assert not (tmp_path / "daimon-agent.pid").exists()


async def test_wait_healthy_timeout_kills_group(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_health", _noop_health(False))
    async def no_sleep(*a):
        return None
    monkeypatch.setattr(client.asyncio, "sleep", no_sleep)
    killed: list[int] = []
    monkeypatch.setattr(client, "_kill_group", lambda pid: killed.append(pid))
    err_log = tmp_path / "err.log"
    err_log.write_text("[daimon-agent] boom\n", encoding="utf-8")
    with pytest.raises(client.ClientError, match="did not become healthy"):
        await client._wait_healthy(4711, _FakeProc(), err_log=err_log, timeout=0.05)
    assert killed == [4242]


async def test_wait_healthy_early_exit_raises_without_killing(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_health", _noop_health(False))
    killed: list[int] = []
    monkeypatch.setattr(client, "_kill_group", lambda pid: killed.append(pid))
    with pytest.raises(client.ClientError, match="exited early"):
        await client._wait_healthy(4711, _FakeProc(poll_result=3), err_log=tmp_path / "err.log")
    assert killed == []


async def test_stop_server_no_pidfile(tmp_path):
    ok, msg = await client.stop_server(run_dir=tmp_path)
    assert ok is False
    assert "no pidfile" in msg


async def test_stop_server_garbage_pidfile(tmp_path):
    (tmp_path / "daimon-agent.pid").write_text("garbage\n", encoding="utf-8")
    ok, _ = await client.stop_server(run_dir=tmp_path)
    assert ok is False


async def test_stop_server_dead_pid_removes_stale_file(monkeypatch, tmp_path):
    pidfile = tmp_path / "daimon-agent.pid"
    pidfile.write_text("4242\n4711\n", encoding="utf-8")
    monkeypatch.setattr(client, "_pid_alive", lambda pid: False)
    ok, msg = await client.stop_server(run_dir=tmp_path)
    assert ok is False
    assert "already dead" in msg
    assert not pidfile.exists()


async def test_stop_server_live_pid_not_answering_kills_hung(monkeypatch, tmp_path):
    # A live pid whose recorded port answers nothing is almost certainly a
    # hung/stuck server.  We kill it so the user can recover (a recycled
    # PID collision is vanishingly unlikely).
    pidfile = tmp_path / "daimon-agent.pid"
    pidfile.write_text("4242\n4711\n", encoding="utf-8")
    monkeypatch.setattr(client, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(client, "_health", _noop_health(False))
    killed: list[int] = []
    monkeypatch.setattr(client, "_kill_group", lambda pid: killed.append(pid))
    ok, msg = await client.stop_server(run_dir=tmp_path)
    assert ok is True
    assert killed == [4242]
    assert "hung" in msg.lower()


async def test_stop_server_kills_only_own_spawned(monkeypatch, tmp_path):
    pidfile = tmp_path / "daimon-agent.pid"
    pidfile.write_text("4242\n4711\n", encoding="utf-8")
    monkeypatch.setattr(client, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(client, "_health", _noop_health(True))
    killed: list[int] = []
    monkeypatch.setattr(client, "_kill_group", lambda pid: killed.append(pid))
    ok, msg = await client.stop_server(run_dir=tmp_path)
    assert ok is True
    assert killed == [4242]
    assert "port 4711" in msg
    assert not pidfile.exists()


# --- introspection helpers ---------------------------------------------------

def test_list_sessions_reads_distinct_thread_ids(tmp_path):
    db = tmp_path / "checkpoints.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE checkpoints (thread_id TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO checkpoints (thread_id) VALUES (?)", [("cli",), ("work",), ("cli",)]
    )
    conn.commit()
    conn.close()
    assert client.list_sessions(db) == ["cli", "work"]


def test_list_sessions_missing_db_and_table(tmp_path):
    assert client.list_sessions(tmp_path / "nope.db") == []
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    assert client.list_sessions(db) == []


def test_agents_cwd_is_the_agents_root():
    root = client._agents_cwd()
    assert (root / "pyproject.toml").exists()


def test_server_checkpoints_db_anchors_relative_to_agents(monkeypatch):
    monkeypatch.setattr(client, "_agents_cwd", lambda: Path("/agents"))
    settings = Settings(checkpoints_db=Path("./memory/checkpoints.db"))
    assert client.server_checkpoints_db(settings) == Path("/agents/memory/checkpoints.db")


def test_server_checkpoints_db_passes_absolute_through():
    settings = Settings(checkpoints_db=Path("/tmp/x.db"))
    assert client.server_checkpoints_db(settings) == Path("/tmp/x.db")
