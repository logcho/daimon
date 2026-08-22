"""The gateway in front of real agent servers.

Everything here runs against a genuine `create_app` behind a genuine
`TestServer`, discovered the way the real thing discovers it — through a run
dir with a pidfile — because the discovery path is most of what could be
wrong.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from langchain_core.messages import AIMessage

from daimon_agent.remote.gateway import Gateway, create_gateway_app
from daimon_agent.server import create_app
from test_server import fake_graph_builder


def _announce(run_root: Path, workspace: Path, port: int) -> str:
    """Write the run-dir record a real server writes on startup."""
    import hashlib

    key = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    run_dir = run_root / key
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "workspace.txt").write_text(f"{workspace}\n")
    (run_dir / "daimon-agent.pid").write_text(f"{os.getpid()}\n{port}\n")
    return key


@pytest.fixture
async def stack(settings, tmp_path: Path):
    """A workspace server, plus a gateway that can find it, plus a paired client."""
    agent_app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(agent_app)) as agent:
        run_root = tmp_path / "run"
        key = _announce(run_root, settings.resolved_workspace_dir.resolve(), agent.port)

        gateway = Gateway(
            state_dir=tmp_path / "remote", run_root=run_root, allow_terminals=True,
        )
        async with TestClient(TestServer(create_gateway_app(gateway))) as client:
            code = gateway.pairing.start()
            paired = await (await client.post("/pair", json={"code": code})).json()
            headers = {"Authorization": f"Bearer {paired['token']}"}
            yield {
                "agent": agent, "agent_app": agent_app, "client": client,
                "gateway": gateway, "key": key, "headers": headers,
            }


async def _open_socket(stack):
    ticket = (await (await stack["client"].post("/ticket", headers=stack["headers"])).json())["ticket"]
    return await stack["client"].ws_connect(f"/ws?ticket={ticket}")


async def _call(ws, op: str, **fields):
    await ws.send_json({"op": op, "op_id": op, **fields})
    frames = []
    while True:
        frame = await asyncio.wait_for(ws.receive_json(), timeout=15)
        if frame.get("kind") == "ack" and frame.get("op_id") == op:
            return frame, frames
        frames.append(frame)


# --- discovery ---------------------------------------------------------------

async def test_a_running_workspace_is_discovered(stack) -> None:
    resp = await stack["client"].get("/workspaces", headers=stack["headers"])
    body = await resp.json()
    assert [w["key"] for w in body["workspaces"]] == [stack["key"]]
    # Named by its directory, which is what the user calls the project — not a
    # hash and not a port.
    assert body["workspaces"][0]["name"]


async def test_a_dead_server_is_not_listed(stack, tmp_path: Path) -> None:
    """A live pid is not enough — the pidfile outlives a SIGKILL, and a
    recycled pid belongs to something else."""
    _announce(tmp_path / "run", Path("/nowhere/at/all"), 1)  # nothing on port 1

    resp = await stack["client"].get("/workspaces", headers=stack["headers"])
    keys = [w["key"] for w in (await resp.json())["workspaces"]]
    assert keys == [stack["key"]]


# --- sessions through the gateway -------------------------------------------

async def test_a_turn_started_locally_streams_to_the_remote_client(stack) -> None:
    """The whole point: the laptop is working, the phone is watching."""
    stack["agent_app"]["graph"].router._pro.script = [AIMessage(content="42")]

    ws = await _open_socket(stack)
    try:
        ack, _ = await _call(ws, "attach", workspace=stack["key"], session="live")
        assert ack["ok"] is True

        resp = await stack["agent"].post(
            "/task", json={"instruction": "what is it?", "session_id": "live"}
        )
        await resp.read()

        seen = []
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if frame.get("kind") != "event":
                continue
            assert frame["workspace"] == stack["key"]  # tagged, so a phone can route it
            seen.append(frame["event"])
            if frame["event"]["type"] == "done":
                break

        assert [e["type"] for e in seen][:2] == ["user", "step"]
        assert seen[-1]["result"] == "42"
    finally:
        await ws.close()


async def test_the_session_directory_comes_through(stack) -> None:
    stack["agent_app"]["graph"].router._pro.script = [AIMessage(content="done")]
    await (await stack["agent"].post(
        "/task", json={"instruction": "some work", "session_id": "s1", "origin": "cli"}
    )).read()

    ws = await _open_socket(stack)
    try:
        ack, _ = await _call(ws, "sessions", workspace=stack["key"])
        titles = {s["session_id"]: s.get("title") for s in ack["sessions"]}
        assert titles["s1"] == "some work"
    finally:
        await ws.close()


async def test_an_op_without_a_workspace_is_refused(stack) -> None:
    ws = await _open_socket(stack)
    try:
        ack, _ = await _call(ws, "attach", session="s")
        assert ack["ok"] is False
        assert "workspace" in ack["error"]
    finally:
        await ws.close()


async def test_an_op_for_a_workspace_that_is_not_running_is_refused(stack) -> None:
    ws = await _open_socket(stack)
    try:
        ack, _ = await _call(ws, "attach", workspace="0" * 16, session="s")
        assert ack["ok"] is False
        assert "not running" in ack["error"]
    finally:
        await ws.close()


async def test_one_socket_reaches_more_than_one_workspace(stack, settings, tmp_path: Path) -> None:
    """A phone shows every project on the machine, not one."""
    from dataclasses import replace

    other_ws = tmp_path / "other-workspace"
    other_ws.mkdir()
    other_settings = replace(
        settings, vault_dir=other_ws, workspace_dir=other_ws,
        memory_db=tmp_path / "other" / "daimon.db",
        checkpoints_db=tmp_path / "other" / "checkpoints.db",
        events_db=tmp_path / "other" / "events.db",
    )
    second_app = await create_app(other_settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(second_app)) as second:
        other_key = _announce(tmp_path / "run", other_ws.resolve(), second.port)

        resp = await stack["client"].get("/workspaces", headers=stack["headers"])
        keys = {w["key"] for w in (await resp.json())["workspaces"]}
        assert keys == {stack["key"], other_key}

        ws = await _open_socket(stack)
        try:
            a, _ = await _call(ws, "attach", workspace=stack["key"], session="s")
            b, _ = await _call(ws, "attach", workspace=other_key, session="s")
            assert (a["ok"], b["ok"]) == (True, True)
        finally:
            await ws.close()


# --- terminals through the gateway ------------------------------------------

async def test_a_terminal_can_be_driven_remotely(stack) -> None:
    ws = await _open_socket(stack)
    try:
        ack, _ = await _call(ws, "term.open", workspace=stack["key"], cols=100, rows=30)
        assert ack["ok"] is True
        term_id = ack["terminal"]["id"]
        await _call(ws, "term.attach", workspace=stack["key"], id=term_id, cols=100, rows=30)

        buf = bytearray()

        async def poll() -> None:
            while b"REMOTE-SHELL" not in bytes(buf):
                await ws.send_json({"op": "term.input", "workspace": stack["key"],
                                    "id": term_id, "data": "printf 'REMOTE-%s\\n' SHELL\n"})
                deadline = asyncio.get_event_loop().time() + 1.5
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        frame = await asyncio.wait_for(ws.receive_json(), timeout=1.5)
                    except asyncio.TimeoutError:
                        break
                    if frame.get("kind") == "term":
                        buf.extend(base64.b64decode(frame["data"]))
                    elif frame.get("control") == "term_snapshot":
                        buf.extend(base64.b64decode(frame["data"]))
                    if b"REMOTE-SHELL" in bytes(buf):
                        return

        await asyncio.wait_for(poll(), timeout=30)
        assert b"REMOTE-SHELL" in bytes(buf)
    finally:
        await ws.close()
        stack["agent_app"]["terminals"].close_all()


async def test_terminals_are_refused_unless_explicitly_exposed(settings, tmp_path: Path) -> None:
    """A shell runs outside the workspace sandbox everything else respects, so
    a paired device does not get one just by being paired."""
    agent_app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(agent_app)) as agent:
        run_root = tmp_path / "run"
        key = _announce(run_root, settings.resolved_workspace_dir.resolve(), agent.port)
        gateway = Gateway(state_dir=tmp_path / "remote", run_root=run_root)  # no --terminals
        assert gateway.allow_terminals is False

        async with TestClient(TestServer(create_gateway_app(gateway))) as client:
            code = gateway.pairing.start()
            token = (await (await client.post("/pair", json={"code": code})).json())["token"]
            headers = {"Authorization": f"Bearer {token}"}
            ticket = (await (await client.post("/ticket", headers=headers)).json())["ticket"]

            async with client.ws_connect(f"/ws?ticket={ticket}") as ws:
                ack, _ = await _call(ws, "term.open", workspace=key)
                assert ack["ok"] is False
                assert "--terminals" in ack["error"]
                # And nothing was spawned on the way to being refused.
                assert agent_app["terminals"].list() == []

                # Sessions still work — the gate is on terminals, not on the
                # whole socket.
                ack, _ = await _call(ws, "attach", workspace=key, session="s")
                assert ack["ok"] is True


async def test_a_conversation_can_be_started_from_the_remote_client(stack) -> None:
    """The path a phone actually uses: a session id it made up itself, a
    prompt over the socket, and the answer coming back as events. Nothing
    creates a session server-side first — a session *is* a checkpointer thread
    id, so a new one is simply an id nothing has used yet."""
    stack["agent_app"]["graph"].router._pro.script = [AIMessage(content="hello from the agent")]

    ws = await _open_socket(stack)
    try:
        ack, _ = await _call(ws, "attach", workspace=stack["key"],
                             session="brand-new-id", wants_ask=True)
        assert ack["ok"] is True

        ack, _ = await _call(ws, "prompt", workspace=stack["key"],
                             session="brand-new-id", instruction="say hello")
        assert ack["ok"] is True, ack

        seen = []
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=20)
            if frame.get("kind") != "event":
                continue
            seen.append(frame["event"])
            if frame["event"]["type"] in ("done", "error"):
                break

        assert seen[0]["type"] == "user"
        assert seen[-1]["type"] == "done", seen[-1]
        assert seen[-1]["result"] == "hello from the agent"
    finally:
        await ws.close()


async def test_a_new_session_shows_up_in_the_directory_afterwards(stack) -> None:
    """Until a turn runs there is nothing to list, which is why the client has
    to be able to start one without asking the server for a session first."""
    stack["agent_app"]["graph"].router._pro.script = [AIMessage(content="done")]

    ws = await _open_socket(stack)
    try:
        before, _ = await _call(ws, "sessions", workspace=stack["key"])
        assert "made-up" not in [s["session_id"] for s in before["sessions"]]

        # Attach first: `prompt` starts a turn but does not subscribe you to
        # it, so a client that skips this sees its own conversation happen
        # in silence.
        await _call(ws, "attach", workspace=stack["key"], session="made-up")
        await _call(ws, "prompt", workspace=stack["key"],
                    session="made-up", instruction="first thing")
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=20)
            if frame.get("kind") == "control" and frame.get("control") == "workspace_lost":
                raise AssertionError("workspace went away")
            if frame.get("kind") == "event" and frame["event"]["type"] in ("done", "error"):
                break

        after, _ = await _call(ws, "sessions", workspace=stack["key"])
        row = [s for s in after["sessions"] if s["session_id"] == "made-up"][0]
        assert row["title"] == "first thing"
    finally:
        await ws.close()
