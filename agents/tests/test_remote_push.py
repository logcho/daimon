"""Notifications: telling you a session is waiting, when you are not looking.

The point of the design here is what is *not* sent. A push payload travels
through a service run by Apple or Google, and a plan the agent wants approved
can quote anything in the workspace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from daimon_agent.remote import push as push_mod
from daimon_agent.remote.gateway import Gateway, _notify, create_gateway_app
from daimon_agent.remote.discovery import Workspace
from daimon_agent.remote.push import PushStore, Subscription


@pytest.fixture
def store(tmp_path: Path) -> PushStore:
    return PushStore(tmp_path / "remote")


@pytest.fixture
async def paired(tmp_path: Path):
    gateway = Gateway(state_dir=tmp_path / "remote", run_root=tmp_path / "run")
    async with TestClient(TestServer(create_gateway_app(gateway))) as client:
        code = gateway.pairing.start()
        token = (await (await client.post("/pair", json={"code": code})).json())["token"]
        yield client, gateway, {"Authorization": f"Bearer {token}"}


# --- vapid -------------------------------------------------------------------

def test_the_server_key_is_stable_across_restarts(tmp_path: Path) -> None:
    """Regenerating it silently invalidates every subscription ever handed
    out, so the phone stops being notified and nothing says why."""
    first = PushStore(tmp_path / "remote").public_key()
    second = PushStore(tmp_path / "remote").public_key()
    assert first is not None
    assert first == second


def test_the_private_key_is_not_readable_by_anyone_else(store: PushStore) -> None:
    store.public_key()
    assert store.key_path.stat().st_mode & 0o077 == 0


# --- subscriptions -----------------------------------------------------------

def test_subscriptions_survive_a_restart(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "remote")
    store.add(Subscription("phone", "https://push.example/abc", {"p256dh": "k", "auth": "a"}))

    reopened = PushStore(tmp_path / "remote")
    assert [s.endpoint for s in reopened.subscriptions()] == ["https://push.example/abc"]


def test_subscribing_twice_from_one_browser_is_not_two_devices(store: PushStore) -> None:
    for _ in range(2):
        store.add(Subscription("phone", "https://push.example/abc", {"p256dh": "k", "auth": "a"}))
    assert len(store.subscriptions()) == 1


def test_revoking_a_device_stops_its_notifications(store: PushStore) -> None:
    """A phone you deliberately cut off must not keep buzzing."""
    store.add(Subscription("phone", "https://push.example/abc", {"p256dh": "k", "auth": "a"}))
    store.add(Subscription("laptop", "https://push.example/def", {"p256dh": "k", "auth": "a"}))

    store.forget_device("phone")
    assert [s.device_id for s in store.subscriptions()] == ["laptop"]


def test_one_corrupt_row_does_not_silence_every_device(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "remote")
    store.add(Subscription("phone", "https://push.example/abc", {"p256dh": "k", "auth": "a"}))
    path = tmp_path / "remote" / "push.json"
    path.write_text(path.read_text().replace('"subscriptions": [', '"subscriptions": [{"junk": 1}, '))

    assert len(PushStore(tmp_path / "remote").subscriptions()) == 1


def test_notifications_are_off_until_something_subscribes(store: PushStore) -> None:
    """The watcher holds a socket open to every workspace, so it only runs
    when there is somebody to notify."""
    assert store.enabled is False
    store.add(Subscription("phone", "https://push.example/abc", {"p256dh": "k", "auth": "a"}))
    assert store.enabled is True


# --- the endpoints -----------------------------------------------------------

async def test_a_device_can_fetch_the_key_and_subscribe(paired) -> None:
    client, gateway, headers = paired

    resp = await client.get("/push/key", headers=headers)
    assert resp.status == 200
    assert len((await resp.json())["key"]) > 60

    resp = await client.post("/push/subscribe", headers=headers, json={
        "endpoint": "https://push.example/xyz", "keys": {"p256dh": "k", "auth": "a"},
    })
    assert resp.status == 200
    assert len(gateway.push.subscriptions()) == 1


async def test_subscribing_needs_an_endpoint_and_keys(paired) -> None:
    client, _gateway, headers = paired
    resp = await client.post("/push/subscribe", headers=headers, json={"endpoint": "x"})
    assert resp.status == 400


async def test_unsubscribing_removes_it(paired) -> None:
    client, gateway, headers = paired
    await client.post("/push/subscribe", headers=headers, json={
        "endpoint": "https://push.example/xyz", "keys": {"p256dh": "k", "auth": "a"},
    })
    await client.post("/push/unsubscribe", headers=headers,
                      json={"endpoint": "https://push.example/xyz"})
    assert gateway.push.subscriptions() == []


async def test_revoking_a_device_over_http_takes_its_notifications(paired) -> None:
    client, gateway, headers = paired
    await client.post("/push/subscribe", headers=headers, json={
        "endpoint": "https://push.example/xyz", "keys": {"p256dh": "k", "auth": "a"},
    })
    device_id = gateway.tokens.devices()[0].id
    gateway.push.subscriptions()[0].device_id = device_id

    await client.delete(f"/devices/{device_id}", headers=headers)
    assert gateway.push.subscriptions() == []


# --- what gets sent ----------------------------------------------------------

async def test_a_notification_names_the_session_but_not_the_question(monkeypatch, tmp_path) -> None:
    """The payload goes through Apple's or Google's push service, and a plan
    the agent wants approved can quote anything in the workspace."""
    gateway = Gateway(state_dir=tmp_path / "remote", run_root=tmp_path / "run")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gateway.push, "notify",
        lambda title, body, url="/": sent.append((title, body)) or 1,
    )

    await _notify(
        gateway,
        Workspace(key="k", path="/Users/me/secret-project", port=1, pid=1),
        "session-1",
        {
            "type": "ask", "kind": "plan", "header": "Plan",
            "question": "Delete the production database?",
            "plan": "1. rm -rf /var/lib/postgres  # the customer list",
        },
    )

    (title, body) = sent[0]
    blob = f"{title} {body}"
    assert "secret-project" in title  # you can tell which one wants you
    assert "production database" not in blob
    assert "postgres" not in blob
    assert "customer" not in blob


async def test_a_gone_endpoint_is_dropped_rather_than_retried_forever(store, monkeypatch) -> None:
    """A push endpoint that is gone stays gone; retrying it is how a backlog
    builds up."""
    store.add(Subscription("phone", "https://push.example/dead", {"p256dh": "k", "auth": "a"}))

    class Gone(push_mod.WebPushException):
        def __init__(self) -> None:
            super().__init__("gone")
            self.response = type("R", (), {"status_code": 410})()

    def boom(**_kwargs):
        raise Gone()

    monkeypatch.setattr(push_mod, "webpush", boom)
    assert store.notify("t", "b") == 0
    assert store.subscriptions() == []


# --- tunnel ------------------------------------------------------------------

async def test_a_missing_cloudflared_is_reported_not_fatal(monkeypatch) -> None:
    """A gateway that cannot open a tunnel is still a working gateway."""
    from daimon_agent.remote.tunnel import Tunnel

    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert await Tunnel().start(4712) is None


async def test_the_tunnel_url_is_scraped_from_cloudflareds_output(monkeypatch) -> None:
    from daimon_agent.remote import tunnel as tunnel_mod

    class FakeStderr:
        def __init__(self) -> None:
            self.lines = [
                b"2026-08-22 INF Requesting new quick tunnel...\n",
                b"2026-08-22 INF |  https://brave-fox-runs.trycloudflare.com  |\n",
            ]

        async def readline(self) -> bytes:
            return self.lines.pop(0) if self.lines else b""

    class FakeProc:
        returncode = None
        stderr = FakeStderr()

        def terminate(self) -> None:
            self.returncode = 0

        async def wait(self) -> int:
            return 0

    async def fake_exec(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/cloudflared")
    monkeypatch.setattr(tunnel_mod.asyncio, "create_subprocess_exec", fake_exec)

    tunnel = tunnel_mod.Tunnel()
    assert await tunnel.start(4712) == "https://brave-fox-runs.trycloudflare.com"
    await tunnel.stop()


async def test_cloudflared_exiting_without_a_url_is_not_a_hang(monkeypatch) -> None:
    from daimon_agent.remote import tunnel as tunnel_mod

    class FakeProc:
        returncode = None
        stderr = type("S", (), {"readline": staticmethod(lambda: _eof())})()

        def terminate(self) -> None:
            self.returncode = 1

        async def wait(self) -> int:
            return 1

    async def _eof() -> bytes:
        return b""

    async def fake_exec(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/cloudflared")
    monkeypatch.setattr(tunnel_mod.asyncio, "create_subprocess_exec", fake_exec)

    assert await tunnel_mod.Tunnel().start(4712) is None


# --- the whole path ----------------------------------------------------------

async def test_a_real_ask_with_nobody_attached_reaches_the_notifier(
    settings, tmp_path, monkeypatch
) -> None:
    """The feature, end to end and in the state that matters: a turn parks on
    a question while *nothing* is attached to that session.

    This is the case `attach` cannot serve — you would have to know the session
    id before it existed — and it is the only case where a notification is
    worth sending at all.
    """
    import asyncio
    import hashlib
    import os

    from aiohttp.test_utils import TestClient, TestServer
    from langchain_core.messages import AIMessage

    from daimon_agent.server import create_app
    from fakes import tool_call
    from test_server import fake_graph_builder

    agent_app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(agent_app)) as agent:
        workspace = settings.resolved_workspace_dir.resolve()
        key = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
        run_dir = tmp_path / "run" / key
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "workspace.txt").write_text(f"{workspace}\n")
        (run_dir / "daimon-agent.pid").write_text(f"{os.getpid()}\n{agent.port}\n")

        gateway = Gateway(state_dir=tmp_path / "remote", run_root=tmp_path / "run")
        notified: list[tuple[str, str]] = []
        monkeypatch.setattr(
            gateway.push, "notify",
            lambda title, body, url="/": notified.append((title, body)) or 1,
        )
        gateway.push.add(Subscription("phone", "https://push.example/e", {"p256dh": "k", "auth": "a"}))

        async with TestClient(TestServer(create_gateway_app(gateway))):
            # The watcher starts on gateway startup because a subscription
            # already exists; give it a moment to find the workspace.
            for _ in range(100):
                await asyncio.sleep(0.05)
                if agent_app["bus"]._observers:
                    break
            assert agent_app["bus"]._observers, "the gateway never started watching"

            agent_app["graph"].router._pro.script = [
                tool_call("ask_user", {"question": "Which?", "options": "A|first"}, id="c1"),
                AIMessage(content="fine"),
            ]
            resp = await agent.post("/task", json={
                "instruction": "pick one", "session_id": "unwatched", "capabilities": ["ask"],
            })
            await resp.read()

            for _ in range(100):
                await asyncio.sleep(0.05)
                if notified:
                    break

    assert notified, "a parked question produced no notification"
    (title, body) = notified[0]
    assert "waiting on you" in title
    assert "Which?" not in f"{title} {body}"  # the question never leaves the machine


def test_the_vapid_subject_is_one_a_push_service_will_accept(tmp_path: Path) -> None:
    """Apple answers `403 BadJwtToken` to a `sub` whose domain does not look
    routable — `mailto:daimon@localhost` among them — and the only symptom is
    notifications never arriving. py_vapid additionally refuses anything that
    is not a mailto, so an https URL is not a way out.
    """
    subject = PushStore(tmp_path / "remote").subject
    assert subject.startswith("mailto:")
    local, _, domain = subject.removeprefix("mailto:").partition("@")
    assert local and "." in domain, subject
    assert domain not in ("localhost", "local"), subject


def test_the_subject_can_be_pointed_at_a_real_contact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DAIMON_PUSH_SUBJECT", "mailto:someone@example.org")
    assert PushStore(tmp_path / "remote").subject == "mailto:someone@example.org"


async def test_a_finished_turn_notifies_without_quoting_the_result(monkeypatch, tmp_path) -> None:
    """The one notification worth having on a long unattended run is "the thing
    you left running is done" — and it was the one that never arrived, because
    the gateway dropped every event that was not an ask.

    The result is withheld for the same reason the question is: it goes through
    somebody else's push service and can quote anything in the workspace.
    """
    gateway = Gateway(state_dir=tmp_path / "remote", run_root=tmp_path / "run")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gateway.push, "notify",
        lambda title, body, url="/": sent.append((title, body)) or 1,
    )

    await _notify(
        gateway,
        Workspace(key="k", path="/Users/me/secret-project", port=1, pid=1),
        "session-1",
        {"type": "done", "result": "the admin password is hunter2"},
    )

    (title, body) = sent[0]
    assert "secret-project" in title
    assert "finished" in title
    assert "hunter2" not in f"{title} {body}"


async def test_an_errored_turn_says_so(monkeypatch, tmp_path) -> None:
    gateway = Gateway(state_dir=tmp_path / "remote", run_root=tmp_path / "run")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gateway.push, "notify",
        lambda title, body, url="/": sent.append((title, body)) or 1,
    )

    await _notify(
        gateway,
        Workspace(key="k", path="/Users/me/proj", port=1, pid=1),
        "session-1",
        {"type": "error", "message": "provider refused the request"},
    )

    (title, body) = sent[0]
    assert "stopped" in title
    assert "refused" not in f"{title} {body}"
