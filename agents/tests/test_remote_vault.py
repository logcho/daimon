"""Reading and writing notes from another device.

The confinement is the point. A note name from a phone is attacker-controlled
input in a way it never was from a local UI, and it goes through exactly the
same resolve-then-check as the HTTP routes — one copy, so the two cannot drift
into one being weaker.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from daimon_agent.server import create_app
from test_server import fake_graph_builder


@pytest.fixture
async def client(settings):
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        yield client


async def _call(ws, op: str, **fields) -> dict:
    await ws.send_json({"op": op, "op_id": op, **fields})
    while True:
        frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
        if frame.get("kind") == "ack" and frame.get("op_id") == op:
            return frame


async def test_notes_can_be_listed_and_read(client: TestClient) -> None:
    root = client.app["settings"].vault_dir
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "kickoff.md").write_text("# Kickoff\n\nthe plan")

    async with client.ws_connect("/bus/ws") as ws:
        listed = await _call(ws, "vault.list")
        assert [n["name"] for n in listed["notes"]] == ["notes/kickoff.md"]

        read = await _call(ws, "vault.read", name="notes/kickoff.md")
        assert read["content"] == "# Kickoff\n\nthe plan"


async def test_a_note_can_be_written_from_a_phone(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        written = await _call(ws, "vault.write", name="thoughts/idea.md", content="jotted down")
        assert written["ok"] is True

    assert (client.app["settings"].vault_dir / "thoughts" / "idea.md").read_text() == "jotted down"


async def test_writing_a_note_keeps_recall_in_step(client: TestClient) -> None:
    """A note the agent cannot find again is half-saved."""
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "vault.write", name="findme.md", content="the tuna casserole recipe")

    assert client.app["memory"].search_notes("casserole")


async def test_a_name_cannot_walk_out_of_the_vault(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        for name in ["../escaped.md", "../../etc/passwd.md", "notes/../../escaped.md"]:
            ack = await _call(ws, "vault.write", name=name, content="should not land")
            assert ack["ok"] is False, name
            assert "outside the vault" in ack["error"], name

        ack = await _call(ws, "vault.read", name="../../etc/passwd")
        assert ack["ok"] is False

    assert not (client.app["settings"].vault_dir.parent / "escaped.md").exists()


async def test_only_markdown_can_be_written(client: TestClient) -> None:
    """The listing is rglob("*.md"), so anything else is written once and then
    invisible forever."""
    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "vault.write", name="script.sh", content="rm -rf /")
        assert ack["ok"] is False
        assert "must end in .md" in ack["error"]


async def test_internal_paths_are_not_listed_as_notes(client: TestClient) -> None:
    """`.daimon/memory` holds this workspace's own databases, and skills have
    their own view."""
    root = client.app["settings"].vault_dir
    (root / ".daimon").mkdir(parents=True, exist_ok=True)
    (root / ".daimon" / "plumbing.md").write_text("internal")
    (root / "skills" / "thing").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "thing" / "SKILL.md").write_text("a skill")
    (root / "real.md").write_text("a note")

    async with client.ws_connect("/bus/ws") as ws:
        listed = await _call(ws, "vault.list")
    assert [n["name"] for n in listed["notes"]] == ["real.md"]


async def test_reading_a_note_that_is_not_there_says_so(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "vault.read", name="nope.md")
        assert ack["ok"] is False
        assert "no such note" in ack["error"]


# --- skills and config -------------------------------------------------------

async def test_skills_can_be_listed_and_read(client: TestClient) -> None:
    # The global library, not the legacy vault-relative one.
    root = client.app["settings"].resolved_skills_dir / "deploying"
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\nname: deploying\ndescription: how we ship\n---\n\nthe steps"
    )

    async with client.ws_connect("/bus/ws") as ws:
        listed = await _call(ws, "skills")
        assert "deploying" in [s["name"] for s in listed["skills"]]

        read = await _call(ws, "skill.read", name="deploying")
        assert "the steps" in read["content"]


async def test_reading_a_skill_that_is_not_there_says_so(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "skill.read", name="imaginary")
        assert ack["ok"] is False


async def test_config_never_carries_the_api_keys(client: TestClient) -> None:
    """The HTTP route returns booleans rather than values, and going out over
    a network does not make that looser."""
    from dataclasses import replace

    client.app["settings"] = replace(
        client.app["settings"], api_key="sk-secret-value-do-not-leak"
    )

    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "config")

    blob = json.dumps(ack)
    assert "sk-secret-value-do-not-leak" not in blob
    assert ack["config"]["api_key_configured"] is True


# --- deleting ----------------------------------------------------------------

async def test_a_note_can_be_deleted(client: TestClient) -> None:
    root = client.app["settings"].vault_dir
    (root / "gone.md").write_text("delete me")

    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "vault.delete", name="gone.md")
        assert ack["ok"] is True

    assert not (root / "gone.md").exists()


async def test_deleting_a_note_takes_it_out_of_recall(client: TestClient) -> None:
    """A note left in the index reads as the agent making things up."""
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "vault.write", name="temp.md", content="the pickled herring recipe")
        assert client.app["memory"].search_notes("herring")

        await _call(ws, "vault.delete", name="temp.md")
        assert not client.app["memory"].search_notes("herring")


async def test_deleting_a_note_cannot_reach_outside_the_vault(client: TestClient) -> None:
    outside = client.app["settings"].vault_dir.parent / "precious.md"
    outside.write_text("do not delete")

    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "vault.delete", name="../precious.md")
        assert ack["ok"] is False

    assert outside.exists()


async def test_deleting_a_note_that_is_not_there_says_so(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "vault.delete", name="imaginary.md")
        assert ack["ok"] is False
        assert "no such note" in ack["error"]


# --- staying in step ---------------------------------------------------------

async def test_writing_a_note_is_announced_to_other_clients(client: TestClient) -> None:
    """A client holding a list of notes cannot discover that somebody else
    changed one."""
    async with client.ws_connect("/bus/ws") as watcher, client.ws_connect("/bus/ws") as other:
        await _call(watcher, "vault.list")  # the socket is up
        await _call(other, "vault.write", name="shared.md", content="hello")

        frame = await asyncio.wait_for(watcher.receive_json(), timeout=5)
        assert frame["control"] == "vault_changed"
        assert (frame["change"], frame["name"]) == ("written", "shared.md")


async def test_deleting_a_note_is_announced(client: TestClient) -> None:
    (client.app["settings"].vault_dir / "doomed.md").write_text("bye")

    async with client.ws_connect("/bus/ws") as watcher, client.ws_connect("/bus/ws") as other:
        await _call(watcher, "vault.list")
        await _call(other, "vault.delete", name="doomed.md")

        frame = await asyncio.wait_for(watcher.receive_json(), timeout=5)
        assert (frame["change"], frame["name"]) == ("deleted", "doomed.md")


async def test_a_note_written_over_http_is_announced_too(client: TestClient) -> None:
    """The agent's own writes go through the same helper, so a note it saves
    mid-turn shows up without a refresh."""
    async with client.ws_connect("/bus/ws") as watcher:
        await _call(watcher, "vault.list")
        resp = await client.put("/vault/from-http.md", json={"content": "written by a route"})
        assert resp.status == 200

        frame = await asyncio.wait_for(watcher.receive_json(), timeout=5)
        assert (frame["change"], frame["name"]) == ("written", "from-http.md")


async def test_a_refused_write_announces_nothing(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as watcher, client.ws_connect("/bus/ws") as other:
        await _call(watcher, "vault.list")
        ack = await _call(other, "vault.write", name="../escaped.md", content="nope")
        assert ack["ok"] is False

        # A fence: anything the server had to say arrives before this answer.
        await watcher.send_json({"op": "ping", "op_id": "fence"})
        seen = []
        while True:
            frame = await asyncio.wait_for(watcher.receive_json(), timeout=5)
            if frame.get("kind") == "ack":
                break
            seen.append(frame)
        assert seen == []


async def test_an_image_can_be_fetched_as_bytes(client: TestClient) -> None:
    """A phone has no filesystem and no asset protocol — an image referenced
    by a note comes down the socket it already has."""
    root = client.app["settings"].vault_dir
    (root / "images").mkdir(parents=True, exist_ok=True)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    (root / "images" / "shot.png").write_bytes(png)

    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "vault.blob", name="images/shot.png")

    assert ack["ok"] is True
    assert ack["mime"] == "image/png"
    assert ack["sizeBytes"] == len(png)
    assert base64.b64decode(ack["base64"]) == png


async def test_blob_reads_cannot_escape_the_vault(client: TestClient) -> None:
    """Same confinement as every other note op — one resolve-then-check, so
    the two cannot drift into one being weaker."""
    async with client.ws_connect("/bus/ws") as ws:
        for name in ("../escaped.png", "../../etc/passwd"):
            ack = await _call(ws, "vault.blob", name=name)
            assert ack["ok"] is False


async def test_blob_will_not_reach_into_internal_paths(client: TestClient) -> None:
    """`.daimon` holds this workspace's own databases and `skills` has its own
    view; neither is listed, so neither should be readable by guessing."""
    root = client.app["settings"].vault_dir
    (root / ".daimon").mkdir(parents=True, exist_ok=True)
    (root / ".daimon" / "memory.db").write_bytes(b"sqlite")

    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "vault.blob", name=".daimon/memory.db")
    assert ack["ok"] is False


async def test_an_oversized_file_is_refused_with_its_size(client: TestClient) -> None:
    """Refused deliberately rather than left to blow the frame limit, which
    fails the whole socket instead of the one request."""
    root = client.app["settings"].vault_dir
    (root / "huge.bin").write_bytes(b"\x00" * (9 * 1024 * 1024))

    async with client.ws_connect("/bus/ws") as ws:
        ack = await _call(ws, "vault.blob", name="huge.bin")
    assert ack["ok"] is False
    assert "too large" in ack["error"]
