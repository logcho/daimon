"""`GET /bus/ws` — the socket a second client watches a session over."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer
from langchain_core.messages import AIMessage

from daimon_agent.events import done_event, step_event, user_event
from daimon_agent.server import create_app
from test_server import fake_graph_builder


@pytest.fixture
async def client(settings):
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        yield client


async def _call(ws, op: str, **fields) -> dict:
    """Send one op and read frames until its ack, returning (ack, frames)."""
    await ws.send_json({"op": op, "op_id": op, **fields})
    frames = []
    while True:
        frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
        if frame["kind"] == "ack" and frame.get("op_id") == op:
            return frame, frames
        frames.append(frame)


async def test_attaching_replays_what_already_happened(client: TestClient) -> None:
    emit = client.app["bus"].publisher("s")
    emit(user_event("what happened?"))
    emit(step_event("1", "Thinking", "done"))

    async with client.ws_connect("/bus/ws") as ws:
        ack, frames = await _call(ws, "attach", session="s")
        assert ack["ok"] is True
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert [e["type"] for e in snapshot["events"]] == ["user", "step"]
        assert snapshot["seq"] == 2


async def test_a_cursor_inside_the_ring_replays_exactly_the_tail(client: TestClient) -> None:
    emit = client.app["bus"].publisher("s")
    for i in range(5):
        emit(step_event(str(i), "t", "done"))

    async with client.ws_connect("/bus/ws") as ws:
        _ack, frames = await _call(ws, "attach", session="s", since=3)
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert [e["id"] for e in snapshot["events"]] == ["3", "4"]
        assert snapshot["truncated"] is False


async def test_attaching_with_no_cursor_is_a_clean_start(client: TestClient) -> None:
    """`truncated` tells the client to reset rather than splice — a fresh
    snapshot does not join up with whatever it had before."""
    client.app["bus"].publisher("s")(done_event("x"))
    async with client.ws_connect("/bus/ws") as ws:
        _ack, frames = await _call(ws, "attach", session="s")
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert snapshot["truncated"] is True


async def test_a_watcher_sees_a_turn_it_did_not_start(client: TestClient) -> None:
    """The whole point: the phone is attached, the laptop starts a turn."""
    client.app["graph"].router._pro.script = [AIMessage(content="42")]

    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="live")

        resp = await client.post(
            "/task", json={"instruction": "what is it?", "session_id": "live"}
        )
        await resp.read()

        seen, seqs = [], []
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
            if frame["kind"] != "event":
                continue
            seen.append(frame["event"])
            seqs.append(frame["seq"])
            if frame["event"]["type"] == "done":
                break

        assert [e["type"] for e in seen][:2] == ["user", "step"]
        assert seen[-1]["result"] == "42"
        # Contiguous, with no gap — a client can trust its cursor to mean
        # "everything up to here", which is what reconnecting depends on.
        assert seqs == list(range(1, len(seqs) + 1))


async def test_events_carry_a_monotonic_sequence(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="s")
        emit = client.app["bus"].publisher("s")
        for i in range(3):
            emit(step_event(str(i), "t", "done"))

        seqs = []
        for _ in range(3):
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
            seqs.append(frame["seq"])
        assert seqs == [1, 2, 3]


async def test_one_socket_can_watch_several_sessions(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="a")
        await _call(ws, "attach", session="b")
        client.app["bus"].publisher("a")(done_event("from a"))
        client.app["bus"].publisher("b")(done_event("from b"))

        got = {}
        for _ in range(2):
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
            got[frame["session"]] = frame["event"]["result"]
        assert got == {"a": "from a", "b": "from b"}


async def test_detaching_stops_the_stream(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="s")
        ack, _ = await _call(ws, "detach", session="s")
        assert ack["ok"] is True
        assert client.app["bus"].channel("s").subscriber_count == 0


async def test_attaching_twice_to_one_session_is_refused(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="s")
        ack, _ = await _call(ws, "attach", session="s")
        assert ack["ok"] is False
        assert "already attached" in ack["error"]


async def test_a_bad_op_is_answered_not_fatal(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "nonsense")
        assert ack["ok"] is False
        # The socket still works.
        ack, _ = await _call(ws, "ping")
        assert ack["ok"] is True


async def test_attach_without_a_session_is_refused(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "attach")
        assert ack["ok"] is False


async def test_closing_the_socket_releases_every_subscription(client: TestClient) -> None:
    """Nothing tells the server when a phone goes into a tunnel; the socket
    closing has to be enough, or subscribers leak and the server never idles
    out."""
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="a")
        await _call(ws, "attach", session="b")

    for _ in range(50):
        await asyncio.sleep(0.02)
        if all(c.subscriber_count == 0 for c in client.app["bus"].channels()):
            break
    assert [c.subscriber_count for c in client.app["bus"].channels()] == [0, 0]


async def test_the_session_directory_lists_live_and_recorded_sessions(client: TestClient) -> None:
    client.app["graph"].router._pro.script = [AIMessage(content="done")]
    await (await client.post(
        "/task", json={"instruction": "recorded work", "session_id": "recorded", "origin": "cli"}
    )).read()
    client.app["bus"].publisher("live-only")(step_event("1", "t", "running"))

    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "sessions")

    by_id = {s["session_id"]: s for s in ack["sessions"]}
    assert by_id["recorded"]["title"] == "recorded work"
    assert by_id["recorded"]["origin"] == "cli"
    assert "live-only" in by_id  # never written to the log, still listed
    assert all(s["busy"] is False for s in by_id.values())


async def test_a_session_parked_on_a_question_says_so(client: TestClient) -> None:
    """The badge that makes remote worth opening: which sessions want you."""
    from daimon_agent.events import ask_event

    client.app["bus"].publisher("parked")(ask_event("a1", "question", "which?", []))

    async with client.ws_connect("/bus/ws") as ws:
        _ack, frames = await _call(ws, "attach", session="parked")
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert snapshot["pending_ask"]["id"] == "a1"

        ack, _ = await _call(ws, "sessions")
        assert [s for s in ack["sessions"] if s["session_id"] == "parked"][0]["pending_ask"] is True


async def test_todos_come_with_the_snapshot(client: TestClient) -> None:
    from daimon_agent.events import todo_event

    items = [{"text": "step one", "status": "in_progress"}]
    client.app["bus"].publisher("s")(todo_event(items))

    async with client.ws_connect("/bus/ws") as ws:
        _ack, frames = await _call(ws, "attach", session="s")
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert snapshot["todos"] == items
