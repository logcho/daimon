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
from daimon_agent.remote.gateway import Gateway, _notify_ask, create_gateway_app
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

    await _notify_ask(
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
