"""The HTTP layer — aiohttp app with a fake router (real graph, no model,
no network): /health, POST /task NDJSON ending in done, same-session
continuation, and the 400s. The event stream must match events.ts exactly
(shapes are frozen in test_events.py; this test checks the wire format)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections import Counter
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from langchain_core.messages import AIMessage

from daimon_agent import server
from daimon_agent.graph import build_graph, close_checkpointer, make_sqlite_checkpointer
from daimon_agent.server import create_app
from fakes import FakeRouter


async def fake_graph_builder(settings, *, memory=None):
    """Real compiled graph over a scripted model — run_turn needs genuine
    astream/aget_state/checkpointer, which only the real graph provides."""
    router = FakeRouter()
    checkpointer = await make_sqlite_checkpointer(settings.resolved_checkpoints_db)
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
    # The prompt itself leads the stream, so a client replaying a session it
    # never saw gets both halves of the conversation.
    assert types[0] == "user"
    assert events[0]["text"] == "What is 2+2?"
    assert types[1] == "step"
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


async def test_clearing_a_session_forgets_its_history(client: TestClient) -> None:
    """`/clear` in the CLI is only honest if the next turn starts blank."""
    graph = client.app["graph"]
    graph.router._pro.script = [
        AIMessage(content="first turn"),
        AIMessage(content="after the clear"),
    ]

    await _post_task(client, "remember X", session_id="s1")
    resp = await client.delete("/sessions/s1")
    assert resp.status == 200
    assert await resp.json() == {"ok": True, "session": "s1"}

    await _post_task(client, "what did you remember?", session_id="s1")
    second_turn_messages = graph.router._pro.calls[-1]
    assert not any(
        "remember X" in getattr(m, "content", "") for m in second_turn_messages
    )
    assert not any(
        "first turn" in getattr(m, "content", "") for m in second_turn_messages
    )


async def test_clearing_one_session_leaves_the_others_alone(client: TestClient) -> None:
    graph = client.app["graph"]
    graph.router._pro.script = [AIMessage(content=f"turn {i}") for i in range(3)]

    await _post_task(client, "remember X", session_id="s1")
    await _post_task(client, "remember Y", session_id="s2")
    assert (await client.delete("/sessions/s1")).status == 200

    await _post_task(client, "and?", session_id="s2")
    assert any(
        "remember Y" in getattr(m, "content", "")
        for m in graph.router._pro.calls[-1]
    )


async def test_clearing_an_unknown_session_is_fine(client: TestClient) -> None:
    """Nothing stored is the asked-for state already — not a 404."""
    resp = await client.delete("/sessions/never-used")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True


async def test_clearing_is_refused_while_a_turn_is_running(client: TestClient) -> None:
    """The running turn holds its message list in memory and would write the
    history straight back — better to say so than to clear nothing."""
    client.app["locks"]["busy"] = asyncio.Lock()
    async with client.app["locks"]["busy"]:
        resp = await client.delete("/sessions/busy")
    assert resp.status == 409
    assert "turn is running" in (await resp.json())["error"]


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


# --- /resume: answering a question the agent asked ---------------------------

async def _post(client: TestClient, path: str, body: dict) -> list[dict]:
    resp = await client.post(path, json=body)
    assert resp.status == 200, await resp.text()
    data = await resp.read()
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


async def test_task_ending_in_ask_then_resume_finishes_the_turn(client: TestClient) -> None:
    """The full bidirectional loop over the wire: /task closes on an `ask`,
    /resume carries the answer back and the same turn completes."""
    from fakes import tool_call

    graph = client.app["graph"]
    graph.router._pro.script = [
        tool_call("ask_user", {"question": "Which?", "options": "A|first\nB|second"}, id="c-ask"),
        AIMessage(content="Going with A."),
    ]

    events = await _post(client, "/task", {
        "instruction": "pick one", "session_id": "resume-1", "capabilities": ["ask"],
    })
    assert events[-1]["type"] == "ask"
    assert events[-1]["question"] == "Which?"
    assert [o["label"] for o in events[-1]["options"]] == ["A", "B"]
    # `ask` is terminal and exclusive — no done alongside it.
    assert not any(e["type"] == "done" for e in events)

    events = await _post(client, "/resume", {
        "session_id": "resume-1", "ask_id": events[-1]["id"], "answer": "A",
    })
    assert events[-1]["type"] == "done"
    assert events[-1]["result"] == "Going with A."


async def test_ask_tools_are_withheld_from_clients_that_cannot_answer(client: TestClient) -> None:
    """No `capabilities` means no way to answer, so the turn must finish
    rather than park on a question — this is what keeps the chat app working
    unchanged against the same server."""
    from fakes import tool_call

    graph = client.app["graph"]
    graph.router._pro.script = [
        tool_call("ask_user", {"question": "Which?", "options": "A|x"}, id="c-ask"),
        AIMessage(content="decided myself"),
    ]
    events = await _post(client, "/task", {"instruction": "go", "session_id": "nocap-1"})
    assert events[-1]["type"] == "done"
    assert not any(e["type"] == "ask" for e in events)


async def test_resume_requires_an_answer(client: TestClient) -> None:
    resp = await client.post("/resume", json={"session_id": "x"})
    assert resp.status == 400


async def test_resume_bad_json_returns_400(client: TestClient) -> None:
    resp = await client.post(
        "/resume", data=b"not json", headers={"content-type": "application/json"}
    )
    assert resp.status == 400


async def test_done_carries_usage_totals(client: TestClient) -> None:
    graph = client.app["graph"]
    graph.router._pro.script = [AIMessage(content="hi")]
    events = await _post(client, "/task", {"instruction": "hi", "session_id": "usage-1"})
    assert "usage" in events[-1]
    assert "input_tokens" in events[-1]["usage"]


async def test_client_disconnect_cancels_the_turn(settings) -> None:
    """Esc in the CLI aborts the request. The server must treat that as a
    cancel — an abandoned turn otherwise keeps calling the model and billing
    for output nobody will read."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_graph_builder(settings, *, memory=None):
        graph, checkpointer, router = await fake_graph_builder(settings, memory=memory)
        original = router._pro._astream

        async def blocking(*args, **kwargs):
            started.set()
            await release.wait()  # never released — the disconnect must win
            async for chunk in original(*args, **kwargs):
                yield chunk

        router._pro._astream = blocking
        return graph, checkpointer, router

    app = await create_app(settings, graph_builder=slow_graph_builder)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/task", json={"instruction": "slow", "session_id": "cancel-1"}
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        resp.close()  # the user pressed Esc

        # The registry drains, which only happens if the turn was cancelled.
        for _ in range(100):
            await asyncio.sleep(0.05)
            if app["turn_registry"]["count"] == 0:
                break
        assert app["turn_registry"]["count"] == 0, "the abandoned turn kept running"


# --- /skills -----------------------------------------------------------------

def _write_skill(root, name: str, description: str, body: str = "the body") -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")


async def test_skills_endpoint_merges_both_libraries(client: TestClient) -> None:
    settings = client.app["settings"]
    _write_skill(settings.resolved_skills_dir, "changelog", "Write a changelog")
    _write_skill(settings.project_skills_dir, "deploy", "Ship this repo")

    resp = await client.get("/skills")
    assert resp.status == 200
    skills = {s["name"]: s for s in await resp.json()}
    assert skills["changelog"]["source"] == "vault"
    assert skills["deploy"]["source"] == "project"
    assert skills["changelog"]["description"] == "Write a changelog"


async def test_skills_endpoint_rescans_every_request(client: TestClient) -> None:
    """A skill the agent just saved has to be visible now — the startup scan
    left it invisible until restart."""
    assert await (await client.get("/skills")).json() == []
    _write_skill(client.app["settings"].resolved_skills_dir, "fresh", "Just saved")
    assert [s["name"] for s in await (await client.get("/skills")).json()] == ["fresh"]


async def test_skill_endpoint_returns_the_body(client: TestClient) -> None:
    _write_skill(
        client.app["settings"].resolved_skills_dir, "changelog", "Write it", "## Categories"
    )
    resp = await client.get("/skills/changelog")
    assert resp.status == 200
    body = await resp.json()
    assert "## Categories" in body["content"]
    assert body["source"] == "vault"


async def test_unknown_skill_is_404(client: TestClient) -> None:
    assert (await client.get("/skills/nope")).status == 404


# --- config: keys and model selection ----------------------------------------

async def test_key_field_routes_by_prefix(client: TestClient, tmp_path, monkeypatch) -> None:
    """Anthropic keys start sk-ant-; DeepSeek's are plain sk-. Detecting it
    server-side means the CLI and the app both work with one field and nobody
    has to be told which box to use."""
    monkeypatch.chdir(tmp_path)

    assert (await client.post("/config", json={"key": "sk-ant-abc123"})).status == 200
    cfg = await (await client.get("/config")).json()
    assert cfg["providers"]["anthropic"]["key_configured"] is True

    assert (await client.post("/config", json={"key": "sk-deepseek-xyz"})).status == 200
    cfg = await (await client.get("/config")).json()
    assert cfg["providers"]["deepseek"]["key_configured"] is True


async def test_model_is_writable(client: TestClient, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    await client.post("/config", json={"key": "sk-deepseek-xyz"})
    resp = await client.post("/config", json={"model": "deepseek-reasoner"})
    assert resp.status == 200
    assert (await (await client.get("/config")).json())["model"] == "deepseek-reasoner"


async def test_an_uninstalled_provider_is_rejected(client: TestClient, tmp_path, monkeypatch) -> None:
    """Accepting an unusable model would leave every later turn failing at the
    API call with nothing pointing back here. The missing *package* is reported
    first — it's the more fundamental of the two problems, and has a different
    fix from a missing key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "daimon_agent.server.provider_available", lambda name: name != "anthropic"
    )
    resp = await client.post("/config", json={"model": "anthropic:claude-sonnet-5"})
    assert resp.status == 400
    assert "uv sync --extra anthropic" in (await resp.json())["error"]
    # And nothing changed.
    assert (await (await client.get("/config")).json())["model"] != "anthropic:claude-sonnet-5"


async def test_an_installed_provider_without_a_key_is_rejected(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """The other half: the package is there, the key isn't — a different fix,
    so a different message."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("daimon_agent.server.provider_available", lambda name: True)
    resp = await client.post("/config", json={"model": "anthropic:claude-sonnet-5"})
    assert resp.status == 400
    assert "no anthropic API key" in (await resp.json())["error"]


async def test_an_unknown_provider_is_rejected(client: TestClient, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    resp = await client.post("/config", json={"model": "wizard:gpt-9"})
    assert resp.status == 400


async def test_a_key_and_model_can_be_set_together(client: TestClient, tmp_path, monkeypatch) -> None:
    """Validation runs against the settings this request produces, so adding a
    key and pointing a model at it works in one call."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = await client.post(
        "/config", json={"key": "sk-ant-abc", "model": "anthropic:claude-sonnet-5"}
    )
    # Accepted only if the provider package is installed; either way the
    # failure must name the missing *package*, not the key we just supplied.
    if resp.status != 200:
        assert "isn't installed" in (await resp.json())["error"]
    else:
        assert (await (await client.get("/config")).json())["model"].startswith("anthropic:")


async def test_empty_config_update_is_rejected(client: TestClient) -> None:
    assert (await client.post("/config", json={})).status == 400


# --- /models -----------------------------------------------------------------

async def test_models_endpoint_covers_every_provider(client: TestClient) -> None:
    """One endpoint for the CLI and the app, so a model offered in one is
    offered in the other."""
    body = await (await client.get("/models")).json()
    assert set(body) == {"deepseek", "anthropic"}
    for state in body.values():
        assert {"models", "source", "installed", "key_configured"} <= set(state)


async def test_models_are_listed_without_a_key(client: TestClient) -> None:
    """The picker needs options before you've pasted a key — that's exactly
    when you most need to see what's on offer."""
    body = await (await client.get("/models")).json()
    entry = body["anthropic"]
    assert entry["key_configured"] is False
    assert entry["source"] == "catalog"
    assert entry["models"], "a keyless provider must still list something"


async def test_the_configured_model_is_listed(client: TestClient) -> None:
    body = await (await client.get("/models")).json()
    settings = client.app["settings"]
    assert settings.model in body["deepseek"]["models"]


# --- adopting the old vault-relative library ---------------------------------

def _skill(root, name: str, body: str = "body") -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: d\n---\n\n{body}\n")


def test_legacy_skills_are_copied_into_the_global_library(settings, tmp_path) -> None:
    """Moving the library must not orphan what someone already built — they'd
    open the app to an empty list and conclude their skills were gone."""
    from dataclasses import replace

    from daimon_agent.server import adopt_legacy_skills

    cfg = replace(settings, vault_dir=tmp_path / "vault", skills_dir=tmp_path / "global")
    _skill(cfg.vault_dir / "skills", "changelog", "the original")

    assert adopt_legacy_skills(cfg) == 1
    assert (cfg.resolved_skills_dir / "changelog" / "SKILL.md").read_text().endswith(
        "the original\n"
    )
    # Copied, not moved — reverting loses nothing.
    assert (cfg.vault_dir / "skills" / "changelog" / "SKILL.md").is_file()


def test_adoption_never_overwrites_an_existing_library(settings, tmp_path) -> None:
    """Only fires on an empty target, which is what makes it a one-time event
    without a flag to keep in sync."""
    from dataclasses import replace

    from daimon_agent.server import adopt_legacy_skills

    cfg = replace(settings, vault_dir=tmp_path / "vault", skills_dir=tmp_path / "global")
    _skill(cfg.vault_dir / "skills", "changelog", "old")
    _skill(cfg.resolved_skills_dir, "changelog", "current")

    assert adopt_legacy_skills(cfg) == 0
    assert "current" in (cfg.resolved_skills_dir / "changelog" / "SKILL.md").read_text()


def test_adoption_is_a_noop_without_a_legacy_library(settings, tmp_path) -> None:
    from dataclasses import replace

    from daimon_agent.server import adopt_legacy_skills

    cfg = replace(settings, vault_dir=tmp_path / "vault", skills_dir=tmp_path / "global")
    assert adopt_legacy_skills(cfg) == 0


def test_adoption_is_a_noop_when_they_are_the_same_directory(settings, tmp_path) -> None:
    from dataclasses import replace

    from daimon_agent.server import adopt_legacy_skills

    cfg = replace(settings, vault_dir=tmp_path, skills_dir=tmp_path / "skills")
    _skill(cfg.resolved_skills_dir, "x")
    assert adopt_legacy_skills(cfg) == 0


# --- vault and deletion ------------------------------------------------------

async def test_vault_listing_is_recursive(client: TestClient) -> None:
    """The agent writes to `vault/notes/`, and a top-level-only listing showed
    none of them — which is what the app's own vault walk did."""
    vault = client.app["settings"].vault_dir
    (vault / "notes").mkdir(parents=True, exist_ok=True)
    (vault / "notes" / "a.md").write_text("nested")
    (vault / "top.md").write_text("top")

    names = {n["name"] for n in await (await client.get("/vault")).json()}
    assert names == {"notes/a.md", "top.md"}


async def test_vault_listing_excludes_skills(client: TestClient) -> None:
    """Skills have their own view; showing them as notes would offer a delete
    that removes half a skill."""
    settings = client.app["settings"]
    _skill(settings.vault_dir / "skills", "changelog")
    (settings.vault_dir / "note.md").write_text("x")

    names = {n["name"] for n in await (await client.get("/vault")).json()}
    assert names == {"note.md"}


async def test_note_read_and_delete(client: TestClient) -> None:
    vault = client.app["settings"].vault_dir
    (vault / "notes").mkdir(parents=True, exist_ok=True)
    (vault / "notes" / "a.md").write_text("the content")

    body = await (await client.get("/vault/notes/a.md")).json()
    assert body["content"] == "the content"

    resp = await client.delete("/vault/notes/a.md")
    assert resp.status == 200
    assert not (vault / "notes" / "a.md").exists()
    assert await (await client.get("/vault")).json() == []


async def test_note_paths_cannot_escape_the_vault(client: TestClient) -> None:
    """A path from a client is untrusted — resolve, then check containment,
    so `..` and symlinks can't walk out."""
    for path in ("/vault/../../etc/passwd", "/vault/..%2F..%2Fetc%2Fpasswd"):
        assert (await client.get(path)).status in (400, 404)
        assert (await client.delete(path)).status in (400, 404)
        assert (await client.put(path, json={"content": "x"})).status in (400, 404)


async def test_note_write_creates_and_updates(client: TestClient) -> None:
    """One endpoint for create and update — a new note is just a write to a
    name that doesn't exist yet, and parent dirs come along for free."""
    vault = client.app["settings"].vault_dir

    resp = await client.put("/vault/notes/new.md", json={"content": "first"})
    assert resp.status == 200
    assert (vault / "notes" / "new.md").read_text() == "first"
    assert (await resp.json())["name"] == "notes/new.md"

    resp = await client.put("/vault/notes/new.md", json={"content": "second"})
    assert resp.status == 200
    assert (vault / "notes" / "new.md").read_text() == "second"

    listing = await (await client.get("/vault")).json()
    assert [n["name"] for n in listing] == ["notes/new.md"]


async def test_note_write_indexes_for_recall(client: TestClient) -> None:
    """A saved note the agent can't `recall` reads as memory silently losing
    it — the write path has to reindex, like the delete path unindexes."""
    memory = client.app["memory"]
    await client.put("/vault/kiwi.md", json={"content": "secret kiwi content"})
    assert memory.search_notes("kiwi")


async def test_note_write_rejects_non_markdown(client: TestClient) -> None:
    """The listing is rglob("*.md"), so anything else would be written and
    then never shown again."""
    resp = await client.put("/vault/notes/thing.txt", json={"content": "x"})
    assert resp.status == 400
    assert not (client.app["settings"].vault_dir / "notes" / "thing.txt").exists()


async def test_note_write_rejects_a_bad_body(client: TestClient) -> None:
    assert (await client.put("/vault/a.md", data="not json")).status == 400
    assert (await client.put("/vault/a.md", json={"content": 42})).status == 400


# --- folders ------------------------------------------------------------------

async def test_folders_are_listed_even_when_empty(client: TestClient) -> None:
    """A folder you just made holds no .md yet, so the note listing can't
    show it — it would disappear on the next refresh."""
    resp = await client.post("/vault/folders", json={"path": "projects/2026"})
    assert resp.status == 200

    folders = await (await client.get("/vault/folders")).json()
    assert "projects" in folders and "projects/2026" in folders
    # ...and it is genuinely empty: no note listing entry backs it.
    assert await (await client.get("/vault")).json() == []


async def test_internal_directories_are_hidden_from_the_vault(client: TestClient) -> None:
    """`.daimon/memory` holds this workspace's own databases — showing it as a
    folder offers the user something they can't use but can delete."""
    vault = client.app["settings"].vault_dir
    (vault / ".daimon" / "memory").mkdir(parents=True, exist_ok=True)
    (vault / ".daimon" / "notes.md").write_text("internal")
    (vault / "real").mkdir(exist_ok=True)

    folders = await (await client.get("/vault/folders")).json()
    assert folders == ["real"]
    names = [n["name"] for n in await (await client.get("/vault")).json()]
    assert ".daimon/notes.md" not in names


async def test_folder_routes_do_not_shadow_a_real_note(client: TestClient) -> None:
    """`/vault/folders` is registered before the `{name:.*}` catch-all, so a
    note that happens to be called `folders.md` must still be reachable."""
    await client.put("/vault/folders.md", json={"content": "not a folder"})
    body = await (await client.get("/vault/folders.md")).json()
    assert body["content"] == "not a folder"
    assert isinstance(await (await client.get("/vault/folders")).json(), list)


async def test_folder_delete_refuses_a_non_empty_folder(client: TestClient) -> None:
    """Deleting a folder is one click but can take any number of notes with
    it — that is not the same decision as deleting one note."""
    await client.put("/vault/keep/a.md", json={"content": "a"})

    resp = await client.delete("/vault/folders/keep")
    assert resp.status == 409
    assert (await resp.json())["notes"] == 1
    assert (client.app["settings"].vault_dir / "keep" / "a.md").exists()

    resp = await client.delete("/vault/folders/keep?recursive=1")
    assert resp.status == 200
    assert not (client.app["settings"].vault_dir / "keep").exists()


async def test_folder_delete_unindexes_the_notes_it_removes(client: TestClient) -> None:
    memory = client.app["memory"]
    await client.put("/vault/gone/mango.md", json={"content": "distinctive mango text"})
    assert memory.search_notes("mango")

    await client.delete("/vault/folders/gone?recursive=1")
    assert not memory.search_notes("mango")


async def test_folder_delete_rejects_the_root_and_escapes(client: TestClient) -> None:
    assert (await client.delete("/vault/folders/")).status == 400
    assert (await client.delete("/vault/folders/../../etc")).status in (400, 404)


# --- moving notes -------------------------------------------------------------

async def test_move_relocates_a_note_into_a_folder(client: TestClient) -> None:
    vault = client.app["settings"].vault_dir
    await client.put("/vault/loose.md", json={"content": "body"})

    resp = await client.post("/vault/move", json={"from": "loose.md", "to": "archive/loose.md"})
    assert resp.status == 200
    assert not (vault / "loose.md").exists()
    assert (vault / "archive" / "loose.md").read_text() == "body"


async def test_move_reindexes_under_the_new_name(client: TestClient) -> None:
    """The memory index is keyed by name — leaving the old entry makes
    `recall` cite a path that no longer exists."""
    memory = client.app["memory"]
    await client.put("/vault/papaya.md", json={"content": "unmistakable papaya text"})

    await client.post("/vault/move", json={"from": "papaya.md", "to": "fruit/papaya.md"})
    hits = [dict(row)["filename"] for row in memory.search_notes("papaya")]
    assert "fruit/papaya.md" in hits
    assert "papaya.md" not in hits


async def test_move_relocates_a_whole_folder(client: TestClient) -> None:
    """Folders move too — nesting them is half of organising a vault."""
    vault = client.app["settings"].vault_dir
    await client.put("/vault/inbox/a.md", json={"content": "a"})
    await client.put("/vault/inbox/deep/b.md", json={"content": "b"})
    await client.post("/vault/folders", json={"path": "archive"})

    resp = await client.post("/vault/move", json={"from": "inbox", "to": "archive/inbox"})
    assert resp.status == 200
    assert (await resp.json())["notes"] == 2
    assert not (vault / "inbox").exists()
    assert (vault / "archive" / "inbox" / "a.md").read_text() == "a"
    assert (vault / "archive" / "inbox" / "deep" / "b.md").read_text() == "b"


async def test_moving_a_folder_rekeys_every_note_under_it(client: TestClient) -> None:
    memory = client.app["memory"]
    await client.put("/vault/inbox/lychee.md", json={"content": "unmistakable lychee text"})

    await client.post("/vault/move", json={"from": "inbox", "to": "archive/inbox"})
    hits = [dict(row)["filename"] for row in memory.search_notes("lychee")]
    assert "archive/inbox/lychee.md" in hits
    assert "inbox/lychee.md" not in hits


async def test_a_folder_cannot_be_moved_into_itself(client: TestClient) -> None:
    """The destination would move out from under the operation as it runs."""
    await client.put("/vault/projects/a.md", json={"content": "a"})

    assert (
        await client.post("/vault/move", json={"from": "projects", "to": "projects/inner"})
    ).status == 400
    assert (
        await client.post("/vault/move", json={"from": "projects", "to": "projects"})
    ).status in (400, 409)
    assert (client.app["settings"].vault_dir / "projects" / "a.md").exists()


async def test_move_will_not_clobber_or_escape(client: TestClient) -> None:
    await client.put("/vault/one.md", json={"content": "one"})
    await client.put("/vault/two.md", json={"content": "two"})

    assert (await client.post("/vault/move", json={"from": "one.md", "to": "two.md"})).status == 409
    assert (await client.post("/vault/move", json={"from": "nope.md", "to": "x.md"})).status == 404
    # Into `skills/` or a dot-dir is refused: both are invisible to every
    # listing, so the file would be filed where nothing can show it again.
    assert (
        await client.post("/vault/move", json={"from": "one.md", "to": ".daimon/hidden.md"})
    ).status == 400
    assert (
        await client.post("/vault/move", json={"from": "one.md", "to": "skills/hidden.md"})
    ).status == 400
    assert (
        await client.post("/vault/move", json={"from": "one.md", "to": "../../etc/evil.md"})
    ).status == 400
    # Nothing moved.
    assert (client.app["settings"].vault_dir / "one.md").read_text() == "one"


async def test_deleting_a_missing_note_is_404(client: TestClient) -> None:
    assert (await client.delete("/vault/nope.md")).status == 404


async def test_skill_delete_removes_the_whole_directory(client: TestClient) -> None:
    """An installed skill is a directory of files — SKILL.md plus references
    and scripts — so deleting only the markdown would leave the rest behind."""
    root = client.app["settings"].resolved_skills_dir
    _skill(root, "pdf")
    (root / "pdf" / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "pdf" / "scripts" / "fill.py").write_text("code")

    resp = await client.delete("/skills/pdf")
    assert resp.status == 200
    assert not (root / "pdf").exists()
    assert await (await client.get("/skills")).json() == []


async def test_deleting_a_missing_skill_is_404(client: TestClient) -> None:
    assert (await client.delete("/skills/nope")).status == 404


async def test_deleting_a_note_drops_its_memory_index(client: TestClient) -> None:
    """A deleted note that still turns up in `recall` reads as the agent
    inventing one."""
    settings = client.app["settings"]
    (settings.vault_dir / "gone.md").write_text("secret pineapple content")
    memory = client.app["memory"]
    memory.index_note("gone.md", "secret pineapple content")
    assert memory.search_notes("pineapple")

    await client.delete("/vault/gone.md")
    assert not memory.search_notes("pineapple")


# --- idle auto-shutdown -------------------------------------------------------

def _make_registry(*, count: int = 0, last_active_at: float = 0.0) -> dict:
    return {
        "count": count,
        "sessions": Counter(),
        "lock": asyncio.Lock(),
        "last_active_at": last_active_at,
    }


async def test_idle_shutdown_fires_once_threshold_crossed() -> None:
    reg = _make_registry(count=0, last_active_at=time.monotonic() - 1000)
    kills: list[tuple[int, int]] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    await server._idle_shutdown_loop(
        reg, idle_timeout=1.0, poll_interval=0.01,
        sleep=fake_sleep, kill=lambda pid, sig: kills.append((pid, sig)),
    )
    assert kills == [(os.getpid(), signal.SIGTERM)]
    assert sleeps == [0.01]  # returns after the first tick that crosses the threshold


async def test_idle_shutdown_waits_while_active() -> None:
    reg = _make_registry(count=1, last_active_at=time.monotonic())
    kills: list[tuple[int, int]] = []
    ticks = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= 3:
            reg["count"] = 0
            reg["last_active_at"] = time.monotonic() - 1000  # go idle on tick 4

    await server._idle_shutdown_loop(
        reg, idle_timeout=1.0, poll_interval=0.01,
        sleep=fake_sleep, kill=lambda pid, sig: kills.append((pid, sig)),
    )
    # Three polls stayed busy/just-went-idle before the loop caught it and
    # shut down within the same poll that flipped the state.
    assert ticks == 3
    assert kills == [(os.getpid(), signal.SIGTERM)]


async def test_idle_shutdown_disabled_when_zero() -> None:
    reg = _make_registry(count=0, last_active_at=time.monotonic() - 1000)
    called = False

    async def fake_sleep(seconds: float) -> None:
        nonlocal called
        called = True

    await server._idle_shutdown_loop(reg, idle_timeout=0, sleep=fake_sleep)
    assert called is False  # returns immediately, no polling at all


async def test_cleanup_removes_the_pidfile(settings, tmp_path) -> None:
    # Our own pid, because that is what a real server writes and what cleanup
    # now requires before it deletes anything — see the record tests below.
    pidfile = tmp_path / "daimon-agent.pid"
    pidfile.write_text(f"{os.getpid()}\n4711\n", encoding="utf-8")
    app = await create_app(settings, graph_builder=fake_graph_builder, pidfile=pidfile)
    async with TestClient(TestServer(app)):
        assert pidfile.exists()  # still there while the app is up
    assert not pidfile.exists()  # cleanup() removed it on shutdown


# --- one tool set per session, not one per server -----------------------------

async def test_each_session_gets_its_own_graph(settings, tmp_path) -> None:
    """A single server-wide tool set shared three things keyed on the session
    id `build_tools` is handed: the todo list, the IPython kernel, and the read
    registry. Two chat sessions clobbered each other's checklist, saw each
    other's kernel variables, and vouched for each other's file reads."""
    from daimon_agent.server import build_default_graph

    _graph, checkpointer, _router, for_session = await build_default_graph(
        replace(settings, workspace_dir=tmp_path), memory=None
    )
    try:
        a, b = for_session("chat-a"), for_session("chat-b")
        assert a is not b
        # Different tool *instances*, because the closures capture the id.
        assert {t.name for t in a.tools} == {t.name for t in b.tools}
        assert next(t for t in a.tools if t.name == "update_todos") is not next(
            t for t in b.tools if t.name == "update_todos"
        )
    finally:
        await close_checkpointer(checkpointer)


async def test_two_sessions_keep_separate_todo_lists(settings, tmp_path) -> None:
    from daimon_agent.server import build_default_graph
    from daimon_agent.tools.todo import clear_todos, get_todos

    _graph, checkpointer, _router, for_session = await build_default_graph(
        replace(settings, workspace_dir=tmp_path), memory=None
    )
    try:
        a = next(t for t in for_session("chat-a").tools if t.name == "update_todos")
        b = next(t for t in for_session("chat-b").tools if t.name == "update_todos")
        await a.ainvoke({"todos": "in_progress|alpha's work"})
        await b.ainvoke({"todos": "pending|beta's work"})

        assert [i["text"] for i in get_todos("chat-a")] == ["alpha's work"]
        assert [i["text"] for i in get_todos("chat-b")] == ["beta's work"]
    finally:
        clear_todos("chat-a")
        clear_todos("chat-b")
        await close_checkpointer(checkpointer)


async def _session_graph_builder(settings, *, memory=None):
    """Like fake_graph_builder, but with the per-session factory — a real
    compiled graph per session over its own scripted model."""
    checkpointer = await make_sqlite_checkpointer(settings.resolved_checkpoints_db)

    def for_session(session_id: str):
        router = FakeRouter()
        graph = build_graph(settings, router, [], checkpointer=checkpointer)
        graph.router = router
        return graph

    return for_session("server"), checkpointer, FakeRouter(), for_session


async def test_a_turn_builds_a_graph_for_its_own_session(settings) -> None:
    app = await create_app(settings, graph_builder=_session_graph_builder)
    async with TestClient(TestServer(app)) as client:
        await _post_task(client, "hello", session_id="chat-a")
        await _post_task(client, "hello", session_id="chat-b")

        graphs = app["graphs"]
        assert set(graphs) == {"chat-a", "chat-b"}
        assert graphs["chat-a"] is not graphs["chat-b"]

        # A second turn on the same session reuses the graph rather than
        # building a fresh tool set (and a fresh kernel) under it.
        existing = graphs["chat-a"]
        await _post_task(client, "again", session_id="chat-a")
        assert app["graphs"]["chat-a"] is existing


async def test_session_graphs_are_capped(settings, monkeypatch) -> None:
    """Nothing tells the server a chat tab closed — the app closes it locally
    and the turn keeps running — so the cache has to bound itself."""
    monkeypatch.setattr(server, "MAX_SESSION_GRAPHS", 2)
    app = await create_app(settings, graph_builder=_session_graph_builder)
    async with TestClient(TestServer(app)) as client:
        for sid in ("s1", "s2", "s3"):
            await _post_task(client, "hi", session_id=sid)

        # Least-recently-used goes first.
        assert set(app["graphs"]) == {"s2", "s3"}


async def test_an_injected_builder_still_shares_one_graph(settings) -> None:
    """A test that injects its own 3-tuple builder is asking for one scripted
    graph; the per-session path must not quietly replace it."""
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        await _post_task(client, "hello", session_id="chat-a")
        await _post_task(client, "hello", session_id="chat-b")

        assert app["make_session_graph"] is None
        assert app["graphs"] == {}  # nothing per-session was ever built


# --- the vault as a folder of files ------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def test_listing_is_markdown_only_by_default(client: TestClient) -> None:
    """A phone and the agent's own tools both mean "markdown" by "note", so
    widening the default would have widened them too."""
    vault = client.app["settings"].vault_dir
    (vault / "note.md").write_text("a note")
    (vault / "shot.png").write_bytes(PNG)

    names = {n["name"] for n in await (await client.get("/vault")).json()}
    assert names == {"note.md"}


async def test_listing_all_includes_every_file(client: TestClient) -> None:
    vault = client.app["settings"].vault_dir
    (vault / "note.md").write_text("a note")
    (vault / "shot.png").write_bytes(PNG)
    (vault / "sub").mkdir()
    (vault / "sub" / "data.csv").write_text("a,b\n")

    entries = {n["name"]: n for n in await (await client.get("/vault?all=1")).json()}
    assert set(entries) == {"note.md", "shot.png", "sub/data.csv"}
    assert entries["shot.png"]["ext"] == "png"
    assert entries["shot.png"]["sizeBytes"] == len(PNG)


async def test_listing_all_still_hides_internal_paths(client: TestClient) -> None:
    """`?all=1` widens which *files* are shown, not which directories — skills
    have their own view and dot-dirs hold this workspace's databases."""
    vault = client.app["settings"].vault_dir
    _skill(vault / "skills", "changelog")
    (vault / ".daimon").mkdir(exist_ok=True)
    (vault / ".daimon" / "memory.db").write_bytes(b"sqlite")
    (vault / "note.md").write_text("a note")

    names = {n["name"] for n in await (await client.get("/vault?all=1")).json()}
    assert names == {"note.md"}


async def test_upload_writes_bytes_verbatim(client: TestClient) -> None:
    resp = await client.put("/vaultfile/shots/screen.png", data=PNG)
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] and body["name"] == "shots/screen.png"
    # Byte-for-byte: an image that came back re-encoded would be a corrupt file
    # that still looked like a successful import.
    assert (client.app["settings"].vault_dir / "shots" / "screen.png").read_bytes() == PNG


async def test_upload_does_not_index_non_markdown(client: TestClient) -> None:
    """A PNG's bytes in the FTS table would have `recall` citing binary noise."""
    await client.put("/vaultfile/shot.png", data=PNG)
    await client.put("/vaultfile/read.md", data=b"the quick brown fox")

    memory = client.app["memory"]
    assert [hit["filename"] for hit in memory.search_notes("quick brown", 3)] == ["read.md"]
    assert memory.search_notes("PNG", 3) == []


async def test_upload_refuses_internal_and_escaping_paths(client: TestClient) -> None:
    assert (await client.put("/vaultfile/skills/evil.sh", data=b"x")).status == 400
    assert (await client.put("/vaultfile/.daimon/evil.db", data=b"x")).status == 400
    # 400 or 404 like the read/write escape test: some of these the client
    # normalises away before the server ever sees them, which is a fine way
    # for an escape to fail but not one this route can take credit for.
    for path in ("/vaultfile/../escaped.png", "/vaultfile/..%2F..%2Fescaped.png"):
        assert (await client.put(path, data=PNG)).status in (400, 404)
    assert not (client.app["settings"].vault_dir.parent / "escaped.png").exists()


async def test_reading_a_binary_file_says_so(client: TestClient) -> None:
    """It used to decode with errors="replace", so asking for a PNG filled the
    editor with replacement characters instead of failing."""
    await client.put("/vaultfile/shot.png", data=PNG)

    resp = await client.get("/vault/shot.png")
    assert resp.status == 415
    assert (await resp.json())["ext"] == "png"


async def test_a_note_with_one_stray_byte_still_opens(client: TestClient) -> None:
    """The strict decode must not lock a user out of a damaged note — they can
    only fix what they can see."""
    (client.app["settings"].vault_dir / "damaged.md").write_bytes(b"before \xff after")

    resp = await client.get("/vault/damaged.md")
    assert resp.status == 200
    assert "before" in (await resp.json())["content"]


async def test_folder_delete_counts_files_that_are_not_notes(client: TestClient) -> None:
    """The guard used to count `*.md`, so a folder of nothing but images
    reported itself empty and was deleted on the first confirm click."""
    await client.put("/vaultfile/album/one.png", data=PNG)
    await client.put("/vaultfile/album/two.png", data=PNG)

    resp = await client.delete("/vault/folders/album")
    assert resp.status == 409
    body = await resp.json()
    assert body["files"] == 2 and body["notes"] == 0
    assert (client.app["settings"].vault_dir / "album" / "one.png").is_file()

    assert (await client.delete("/vault/folders/album?recursive=1")).status == 200
    assert not (client.app["settings"].vault_dir / "album").exists()


async def test_move_accepts_any_suffix(client: TestClient) -> None:
    await client.put("/vaultfile/notes.txt", data=b"plain")

    resp = await client.post("/vault/move", json={"from": "notes.txt", "to": "kept/notes.txt"})
    assert resp.status == 200
    assert (client.app["settings"].vault_dir / "kept" / "notes.txt").read_text() == "plain"


# --- editing a skill ---------------------------------------------------------


async def test_skill_can_be_edited(client: TestClient) -> None:
    settings = client.app["settings"]
    _skill(settings.skills_dir, "changelog", "the original body")

    resp = await client.put(
        "/skills/changelog",
        json={"content": "---\nname: changelog\ndescription: d\n---\n\nthe edited body\n"},
    )
    assert resp.status == 200
    assert (await resp.json())["name"] == "changelog"
    assert "the edited body" in (await (await client.get("/skills/changelog")).json())["content"]


async def test_editing_the_frontmatter_renames_the_skill(client: TestClient) -> None:
    """The name lives in the frontmatter and *is* the skill's identity — the
    reply carries the name it ended up with, not the one it was asked for."""
    settings = client.app["settings"]
    _skill(settings.skills_dir, "changelog")

    resp = await client.put(
        "/skills/changelog",
        json={"content": "---\nname: release-notes\ndescription: newer\n---\n\nbody\n"},
    )
    assert await resp.json() == {
        "ok": True,
        "name": "release-notes",
        "description": "newer",
        "source": "vault",
    }
    names = {s["name"] for s in await (await client.get("/skills")).json()}
    assert names == {"release-notes"}

    # The old key must not survive in the index, or `recall` goes on citing a
    # skill under a name nothing answers to.
    memory = client.app["memory"]
    assert [s["name"] for s in memory.search_skills("newer", 3)] == ["release-notes"]
    assert not any(s["name"] == "changelog" for s in memory.search_skills("changelog", 3))


async def test_editing_reindexes_the_description(client: TestClient) -> None:
    settings = client.app["settings"]
    _skill(settings.skills_dir, "changelog")

    await client.put(
        "/skills/changelog",
        json={"content": "---\nname: changelog\ndescription: summarising a release\n---\n\nb\n"},
    )
    memory = client.app["memory"]
    assert [s["name"] for s in memory.search_skills("summarising release", 3)] == ["changelog"]


async def test_editing_an_unknown_skill_is_a_404(client: TestClient) -> None:
    assert (await client.put("/skills/nope", json={"content": "x"})).status == 404


async def test_editing_rejects_a_non_string_body(client: TestClient) -> None:
    settings = client.app["settings"]
    _skill(settings.skills_dir, "changelog")
    assert (await client.put("/skills/changelog", json={"content": None})).status == 400


# --- the discovery record ----------------------------------------------------
#
# The pidfile in a workspace's run dir is what `remote/discovery.py` enumerates
# to answer "which agent servers are running on this machine" — the list a phone
# sees. One slot per workspace, so two servers on the same workspace contend for
# it, and only the one currently recorded there may remove it. Removal on exit
# itself is covered by test_cleanup_removes_the_pidfile above.


async def test_a_server_leaves_another_servers_record_alone(settings, tmp_path) -> None:
    # The slot was taken over by a second server on the same workspace (another
    # app instance, a CLI server on the same root). This one is on its way out
    # and no longer owns the record — deleting it stranded the server that does,
    # which stayed up and serving while `discover()` reported nothing at all.
    other = os.getpid() + 1
    pidfile = tmp_path / "daimon-agent.pid"
    pidfile.write_text(f"{other}\n4712\n", encoding="utf-8")

    app = await create_app(settings, graph_builder=fake_graph_builder, pidfile=pidfile)
    async with TestClient(TestServer(app)):
        pass

    assert pidfile.exists(), "a departing server deleted a record it did not own"
    assert pidfile.read_text(encoding="utf-8").splitlines()[0] == str(other)


async def test_a_garbage_record_is_left_alone(settings, tmp_path) -> None:
    # Unreadable means unowned. A stale record is health-probed away by
    # discovery; a deleted one cannot be recovered, so this errs toward keeping.
    pidfile = tmp_path / "daimon-agent.pid"
    pidfile.write_text("not-a-pid\n", encoding="utf-8")

    app = await create_app(settings, graph_builder=fake_graph_builder, pidfile=pidfile)
    async with TestClient(TestServer(app)):
        pass

    assert pidfile.exists()
