"""The HTTP layer — aiohttp app with a fake router (real graph, no model,
no network): /health, POST /task NDJSON ending in done, same-session
continuation, and the 400s. The event stream must match events.ts exactly
(shapes are frozen in test_events.py; this test checks the wire format)."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from langchain_core.messages import AIMessage

from daimon_agent.graph import build_graph, make_sqlite_checkpointer
from daimon_agent.server import create_app
from fakes import FakeRouter


async def fake_graph_builder(settings, *, memory=None):
    """Real compiled graph over a scripted model — run_turn needs genuine
    astream/aget_state/checkpointer, which only the real graph provides."""
    router = FakeRouter()
    checkpointer = await make_sqlite_checkpointer(settings.checkpoints_db)
    graph = build_graph(settings, router, [], checkpointer=checkpointer)
    graph.router = router  # test handle to script the model
    return graph, checkpointer, router


@pytest.fixture
async def client(settings):
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        yield client


async def _post_task(client: TestClient, instruction: str, session_id: str | None = None) -> list[dict]:
    body = {"instruction": instruction}
    if session_id is not None:
        body["session_id"] = session_id
    resp = await client.post("/task", json=body)
    assert resp.status == 200
    assert resp.headers["content-type"] == "application/x-ndjson"
    data = await resp.read()
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


async def test_health(client: TestClient) -> None:
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert "workspace" in body
    assert body["workspace"].endswith("vault")


async def test_task_streams_events_and_ends_with_done(client: TestClient) -> None:
    graph = client.app["graph"]
    graph.router._pro.script = [AIMessage(content="The answer is 42.")]

    events = await _post_task(client, "What is 2+2?")
    types = [e["type"] for e in events]
    assert types[0] == "step"
    assert types[-1] == "done"
    assert events[-1]["result"] == "The answer is 42."
    # Thinking bracketing and a single done — no error event.
    assert "error" not in types
    steps = [e for e in events if e["type"] == "step"]
    assert steps[0]["label"] == "Thinking"
    assert steps[-1]["label"] == "Thinking"


async def test_task_same_session_continues_history(client: TestClient) -> None:
    graph = client.app["graph"]
    graph.router._pro.script = [
        AIMessage(content="first turn"),
        AIMessage(content="second turn sees history"),
    ]

    await _post_task(client, "remember X")
    events = await _post_task(client, "what did you remember?")
    assert [e for e in events if e["type"] == "done"][0]["result"] == "second turn sees history"
    # The checkpointer kept the thread: turn 2's model call saw turn 1's messages.
    calls = graph.router._pro.calls
    assert len(calls) == 2
    second_turn_messages = calls[-1]
    assert any("remember X" in getattr(m, "content", "") for m in second_turn_messages)
    assert any("first turn" in getattr(m, "content", "") for m in second_turn_messages)


async def test_task_per_session_lock_serializes_concurrent_turns(client: TestClient) -> None:
    """Two same-session turns posted concurrently queue on the session lock
    (like legacy's queue); each still ends in done, in order."""
    graph = client.app["graph"]
    graph.router._pro.script = [AIMessage(content=f"turn {i}") for i in range(2)]

    async def post(instruction: str) -> list[dict]:
        resp = await client.post("/task", json={"instruction": instruction})
        data = await resp.read()
        return [json.loads(line) for line in data.decode().splitlines() if line.strip()]

    bodies = await asyncio.gather(post("first"), post("second"))
    for body in bodies:
        assert [e["type"] for e in body][-1] == "done"
    # Scripted answers arrived in lock order.
    assert [e for e in bodies[0] if e["type"] == "done"][0]["result"] == "turn 0"
    assert [e for e in bodies[1] if e["type"] == "done"][0]["result"] == "turn 1"


async def test_task_bad_json_returns_400(client: TestClient) -> None:
    resp = await client.post("/task", data=b"not json", headers={"content-type": "application/json"})
    assert resp.status == 400
    assert await resp.text() == "invalid json body"


async def test_task_missing_instruction_returns_400(client: TestClient) -> None:
    resp = await client.post("/task", json={"session_id": "x"})
    assert resp.status == 400
    assert await resp.text() == "instruction is required"


async def test_config_endpoint(client: TestClient) -> None:
    """GET /config returns non-secret settings as JSON."""
    resp = await client.get("/config")
    assert resp.status == 200
    body = await resp.json()
    assert body["model"] == "deepseek-chat"
    assert body["port"] == 4711
    assert "workspace" in body
    assert body["workspace"].endswith("vault")
    assert isinstance(body["live_frames"], bool)
    assert isinstance(body["reflect"], bool)
    assert isinstance(body["compaction_chars"], int)
    assert isinstance(body["temperature"], (int, float))
    assert isinstance(body["pinchtab_healthy"], bool)
