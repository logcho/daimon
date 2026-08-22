"""Many clients on one turn.

The stream used to belong to whichever socket started it. These tests pin the
consequences of it belonging to the session instead: a second client sees the
same turn, one client leaving no longer ends it for everyone, and a turn queued
behind another does not get handed its predecessor's events.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from langchain_core.messages import AIMessage

from daimon_agent.server import create_app
from test_server import fake_graph_builder


async def _collect(sub, *, timeout: float = 5.0) -> list[dict]:
    """Read a bus subscriber until a terminal event or the sentinel."""
    events: list[dict] = []

    async def read() -> None:
        while True:
            item = await sub.queue.get()
            if item is None:
                return
            events.append(item[1])
            if item[1].get("type") in ("done", "error", "ask"):
                return

    await asyncio.wait_for(read(), timeout=timeout)
    return events


async def test_a_second_client_sees_a_turn_it_did_not_start(settings) -> None:
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        app["graph"].router._pro.script = [AIMessage(content="The answer is 42.")]
        watcher = app["bus"].subscribe("shared")

        resp = await client.post(
            "/task", json={"instruction": "what is it?", "session_id": "shared"}
        )
        streamed = [json.loads(line) for line in (await resp.read()).decode().splitlines() if line.strip()]
        watched = await _collect(watcher)

        assert [e["type"] for e in watched] == [e["type"] for e in streamed]
        assert watched[-1]["result"] == "The answer is 42."


async def test_one_client_leaving_does_not_cancel_a_turn_another_is_watching(settings) -> None:
    """The regression this whole design risks: a phone attaching must not mean
    closing the laptop kills the turn — and closing the phone must not kill
    the laptop's."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_graph_builder(settings, *, memory=None):
        graph, checkpointer, router = await fake_graph_builder(settings, memory=memory)
        original = router._pro._astream

        async def blocking(*args, **kwargs):
            started.set()
            await release.wait()
            async for chunk in original(*args, **kwargs):
                yield chunk

        router._pro._astream = blocking
        return graph, checkpointer, router

    app = await create_app(settings, graph_builder=slow_graph_builder)
    async with TestClient(TestServer(app)) as client:
        app["graph"].router._pro.script = [AIMessage(content="finished anyway")]
        watcher = app["bus"].subscribe("shared")

        resp = await client.post(
            "/task", json={"instruction": "slow", "session_id": "shared"}
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        resp.close()  # this client is gone, the watcher is not
        await asyncio.sleep(0.05)  # let the writer notice the reset
        release.set()

        events = await _collect(watcher)
        assert events[-1]["type"] == "done"
        assert events[-1]["result"] == "finished anyway"


async def test_a_queued_turn_does_not_inherit_the_running_turns_events(settings) -> None:
    """Both requests are for one session, so the second waits on the per-session
    lock. If it subscribed before taking that lock it would be handed the first
    turn's events — including its terminal one, closing the second client's
    stream before its own turn had begun."""
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        app["graph"].router._pro.script = [
            AIMessage(content="first answer"),
            AIMessage(content="second answer"),
        ]

        first, second = await asyncio.gather(
            client.post("/task", json={"instruction": "one", "session_id": "queued"}),
            client.post("/task", json={"instruction": "two", "session_id": "queued"}),
        )
        streams = []
        for resp in (first, second):
            body = (await resp.read()).decode()
            streams.append([json.loads(line) for line in body.splitlines() if line.strip()])

        for events in streams:
            types = [e["type"] for e in events]
            # Exactly one terminal event, and it is the last thing on the wire.
            assert sum(t in ("done", "error", "ask") for t in types) == 1
            assert types[-1] in ("done", "error", "ask")
            # Each client saw its own prompt, and only its own.
            assert [e["text"] for e in events if e["type"] == "user"] in (["one"], ["two"])

        assert {s[0]["text"] for s in streams} == {"one", "two"}


async def test_a_turn_is_recorded_for_a_client_that_arrives_later(settings) -> None:
    """Nobody was attached while this ran. The point of the log is that the
    conversation is still there afterwards."""
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        app["graph"].router._pro.script = [AIMessage(content="recorded")]
        resp = await client.post(
            "/task", json={"instruction": "remember this", "session_id": "later", "origin": "cli"}
        )
        await resp.read()

        app["eventlog"].flush()
        replayed = app["eventlog"].replay("later")
        assert [e["type"] for e in replayed][:2] == ["user", "step"]
        assert replayed[0]["text"] == "remember this"
        assert replayed[-1]["result"] == "recorded"

        (row,) = app["eventlog"].sessions()
        assert (row["session_id"], row["title"], row["origin"]) == ("later", "remember this", "cli")


async def test_forgetting_a_session_clears_its_channel_and_its_record(settings) -> None:
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        app["graph"].router._pro.script = [AIMessage(content="forget me")]
        await (await client.post(
            "/task", json={"instruction": "secret", "session_id": "gone"}
        )).read()
        app["eventlog"].flush()
        assert app["eventlog"].replay("gone")

        resp = await client.delete("/sessions/gone")
        assert resp.status == 200

        assert app["bus"].peek("gone") is None
        assert app["eventlog"].replay("gone") == []
        assert app["eventlog"].sessions() == []
