"""Answering the agent's questions from somewhere else, and stopping it.

The agent parks on a question by suspending in the checkpointer, which is why
the answer can arrive hours later from a device that never saw the question
asked. What is new is that two clients can now both be looking at one parked
session, so answering has to be a claim rather than a request.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from langchain_core.messages import AIMessage

from daimon_agent.server import create_app
from test_server import fake_graph_builder


@pytest.fixture
async def client(settings):
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        yield client


async def _park(client: TestClient, session: str) -> str:
    """Park a session on a real question and return its ask id.

    Driven by the model actually calling an ask tool, not by publishing an
    `ask` event: the thing being tested resumes a *suspended graph*, and a
    session that merely looks parked from the outside has nothing to resume.
    """
    from fakes import tool_call

    client.app["graph"].router._pro.script = [
        tool_call("ask_user", {"question": "Which?", "options": "A|first\nB|second"}, id="c-ask"),
        AIMessage(content="Going with A."),
    ]
    resp = await client.post("/task", json={
        "instruction": "pick one", "session_id": session, "capabilities": ["ask"],
    })
    events = [json.loads(line) for line in (await resp.read()).decode().splitlines() if line.strip()]
    assert events[-1]["type"] == "ask", events[-1]
    return events[-1]["id"]


async def test_a_parked_session_says_what_it_is_waiting_for(client: TestClient) -> None:
    """A client attaching to a session that asked something an hour ago has to
    be able to render the prompt — the event that carried it is long gone."""
    ask_id = await _park(client, "parked")

    async with client.ws_connect("/bus/ws") as ws:
        await ws.send_json({"op": "attach", "op_id": "a", "session": "parked"})
        frames = []
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
            frames.append(frame)
            if frame.get("kind") == "ack":
                break
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert snapshot["pending_ask"]["id"] == ask_id
        assert snapshot["pending_ask"]["question"] == "Which?"


async def test_two_clients_racing_to_answer_produce_one_answer(client: TestClient) -> None:
    """The failure this prevents is not a duplicate message — it is resuming
    the same suspended turn twice."""
    ask_id = await _park(client, "race")

    first = await client.post("/resume", json={
        "session_id": "race", "ask_id": ask_id, "answer": "A",
    })
    body = [json.loads(line) for line in (await first.read()).decode().splitlines() if line.strip()]
    second = await client.post("/resume", json={
        "session_id": "race", "ask_id": ask_id, "answer": "A",
    })

    assert body[-1]["type"] == "done"  # the winner's turn actually resumed
    assert second.status == 409
    assert "already answered" in (await second.json())["error"]


async def test_answering_a_question_that_moved_on_is_refused(client: TestClient) -> None:
    """A phone showing a stale prompt must not answer whatever came next."""
    ask_id = await _park(client, "moved")

    resp = await client.post("/resume", json={
        "session_id": "moved", "ask_id": "a-stale-one", "answer": "yes",
    })
    assert resp.status == 409
    assert (await resp.json())["ask_id"] == ask_id


async def test_a_client_without_an_ask_id_still_works(client: TestClient) -> None:
    """The CLI and the app predate this; they must not start failing."""
    await _park(client, "legacy")

    resp = await client.post("/resume", json={"session_id": "legacy", "answer": "A"})
    body = [json.loads(line) for line in (await resp.read()).decode().splitlines() if line.strip()]
    assert body[-1]["type"] == "done"


async def test_everyone_else_watching_is_told_it_was_answered(client: TestClient) -> None:
    """Otherwise the other client's prompt just sits there, and tapping it
    resumes a turn that already resumed."""
    ask_id = await _park(client, "shared")
    watcher = client.app["bus"].subscribe("shared")

    resp = await client.post("/resume", json={
        "session_id": "shared", "ask_id": ask_id, "answer": "A", "by": "Logan's iPhone",
    })
    await resp.read()

    resolved = None
    while not watcher.queue.empty():
        item = watcher.queue.get_nowait()
        if item and item[1].get("type") == "ask_resolved":
            resolved = item[1]
    assert resolved is not None
    assert resolved["id"] == ask_id
    assert resolved["by"] == "Logan's iPhone"


async def test_answering_over_the_socket_resumes_the_turn(client: TestClient) -> None:
    ask_id = await _park(client, "ws-answer")

    async with client.ws_connect("/bus/ws") as ws:
        await ws.send_json({"op": "attach", "op_id": "a", "session": "ws-answer"})
        await ws.send_json({"op": "answer", "op_id": "b", "session": "ws-answer",
                            "ask_id": ask_id, "answer": "A", "by": "phone"})

        seen = []
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if frame.get("kind") != "event":
                continue
            seen.append(frame["event"])
            if frame["event"]["type"] in ("done", "error"):
                break

        assert any(e["type"] == "ask_resolved" for e in seen)
        assert seen[-1]["type"] == "done"
        assert seen[-1]["result"] == "Going with A."


async def test_answering_when_nothing_is_waiting_is_refused(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await ws.send_json({"op": "answer", "op_id": "b", "session": "idle", "answer": "yes"})
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
            if frame.get("kind") == "ack":
                break
        assert frame["ok"] is False


# --- interrupt ---------------------------------------------------------------

async def test_interrupting_an_idle_session_is_a_no_op_not_an_error(client: TestClient) -> None:
    """"Stop" on something already stopped is the state the caller asked for."""
    resp = await client.post("/interrupt", json={"session_id": "nothing"})
    assert resp.status == 200
    assert (await resp.json())["was"] == "idle"


async def test_interrupting_a_running_turn_stops_it(client: TestClient) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    original = client.app["graph"].router._pro._astream

    async def blocking(*args, **kwargs):
        started.set()
        await release.wait()
        async for chunk in original(*args, **kwargs):
            yield chunk

    client.app["graph"].router._pro._astream = blocking
    client.app["graph"].router._pro.script = [AIMessage(content="never gets here")]

    watcher = client.app["bus"].subscribe("stopme")
    async with client.ws_connect("/bus/ws") as ws:
        await ws.send_json({"op": "prompt", "op_id": "p", "session": "stopme",
                            "instruction": "something long"})
        await asyncio.wait_for(started.wait(), timeout=10)

        resp = await client.post("/interrupt", json={"session_id": "stopme", "by": "phone"})
        assert (await resp.json())["was"] == "running"

    # Watchers are told, and told who — a turn that merely stopped emitting
    # would look like the agent died.
    events = []
    async def drain():
        while True:
            item = await watcher.queue.get()
            if item is None:
                return
            events.append(item[1])
            if item[1]["type"] in ("done", "error"):
                return

    await asyncio.wait_for(drain(), timeout=10)
    assert events[-1]["type"] == "error"
    assert "cancelled by phone" in events[-1]["message"]


async def test_the_session_is_usable_again_after_an_interrupt(client: TestClient) -> None:
    """The failure mode worth guarding: a cancel that escapes the per-session
    lock without releasing it makes that session permanently unusable."""
    started = asyncio.Event()
    release = asyncio.Event()
    original = client.app["graph"].router._pro._astream

    async def blocking(*args, **kwargs):
        started.set()
        await release.wait()
        async for chunk in original(*args, **kwargs):
            yield chunk

    client.app["graph"].router._pro._astream = blocking
    client.app["graph"].router._pro.script = [AIMessage(content="one"), AIMessage(content="two")]

    async with client.ws_connect("/bus/ws") as ws:
        await ws.send_json({"op": "prompt", "op_id": "p", "session": "reuse",
                            "instruction": "long one"})
        await asyncio.wait_for(started.wait(), timeout=10)
        await client.post("/interrupt", json={"session_id": "reuse"})

    for _ in range(100):
        await asyncio.sleep(0.05)
        if client.app["turn_registry"]["count"] == 0:
            break
    assert client.app["turn_registry"]["count"] == 0

    client.app["graph"].router._pro._astream = original
    release.set()
    resp = await client.post("/task", json={"instruction": "again", "session_id": "reuse"})
    body = (await resp.read()).decode()
    types = [json.loads(line)["type"] for line in body.splitlines() if line.strip()]
    assert types[-1] == "done"


async def test_interrupting_a_parked_session_unwinds_it_rather_than_killing_it(client: TestClient) -> None:
    """Cancelling a suspended LangGraph thread mid-node corrupts the checkpoint
    it is suspended in, so a parked turn is answered with nothing and allowed
    to unwind through its normal path."""
    await _park(client, "parked-stop")

    resp = await client.post("/interrupt", json={"session_id": "parked-stop"})
    assert (await resp.json())["was"] == "waiting"
    assert client.app["bus"].channel("parked-stop").pending_ask is None


async def test_a_client_that_loses_the_race_is_told_so_specifically(client: TestClient) -> None:
    """The CLI shows "answered on another device" rather than an error, which
    it can only do if losing is distinguishable from failing."""
    from daimon_agent import client as client_mod

    ask_id = await _park(client, "loser")
    await (await client.post("/resume", json={
        "session_id": "loser", "ask_id": ask_id, "answer": "A",
    })).read()

    with pytest.raises(client_mod.AlreadyAnswered):
        await client_mod.resume_turn(
            client.session, client.port, "loser", ask_id, "A", lambda _e: None,
        )
