"""Who can reach the gateway, and what the gateway can reach.

The agent servers are arbitrary code execution on this machine and have no
auth of their own; the only thing between them and the network is this file
being right.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from daimon_agent.remote.auth import (
    CODE_ALPHABET,
    MAX_CODE_ATTEMPTS,
    Pairing,
    TokenStore,
    Tickets,
    hash_token,
)
from daimon_agent.remote.gateway import Gateway, create_gateway_app


@pytest.fixture
def gateway(tmp_path: Path) -> Gateway:
    return Gateway(state_dir=tmp_path / "remote", run_root=tmp_path / "run")


@pytest.fixture
async def client(gateway: Gateway):
    async with TestClient(TestServer(create_gateway_app(gateway))) as client:
        yield client


# --- the surface -------------------------------------------------------------

def test_the_gateway_exposes_exactly_these_routes(gateway: Gateway) -> None:
    """A guard, not a description. The agent server's own routes — /task,
    /vault, /config, /sessions — are arbitrary code execution and arbitrary
    file writes, and the thing keeping them off the network is that they are
    in a different process with no network listener. Nothing here should ever
    grow a route that proxies to one of them wholesale.
    """
    app = create_gateway_app(gateway)
    # HEAD is filtered out, not overlooked: aiohttp registers one for every GET
    # automatically, sharing the same handler and the same auth middleware, so
    # it adds no surface of its own.
    routes = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if r.resource is not None and r.method != "HEAD"
    }
    assert routes == {
        ("GET", "/"),
        ("GET", "/health"),
        ("POST", "/pair"),
        ("POST", "/ticket"),
        ("GET", "/workspaces"),
        ("GET", "/devices"),
        ("DELETE", "/devices/{device_id}"),
        ("GET", "/ws"),
        ("GET", "/manifest.webmanifest"),
        ("GET", "/assets"),
    }


async def test_every_route_but_the_public_ones_needs_a_credential(client: TestClient) -> None:
    for method, path in [
        ("get", "/workspaces"),
        ("get", "/devices"),
        ("post", "/ticket"),
        ("delete", "/devices/abc"),
    ]:
        resp = await getattr(client, method)(path)
        assert resp.status == 401, f"{method.upper()} {path} was not gated"


async def test_health_says_nothing_a_stranger_could_use(client: TestClient) -> None:
    """It has to be reachable unauthenticated so a client can find out whether
    it is talking to a daimon gateway at all — so it must not leak workspace
    paths, session names or device labels."""
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == {"ok", "service", "version", "pairing"}


# --- pairing -----------------------------------------------------------------

async def test_pairing_with_the_right_code_yields_a_token(client: TestClient, gateway: Gateway) -> None:
    code = gateway.pairing.start()
    resp = await client.post("/pair", json={"code": code, "label": "iPhone"})
    assert resp.status == 200
    body = await resp.json()
    assert len(body["token"]) > 30
    assert body["device"]["label"] == "iPhone"


async def test_a_wrong_code_is_refused(client: TestClient, gateway: Gateway) -> None:
    gateway.pairing.start()
    resp = await client.post("/pair", json={"code": "WRONGCOD"})
    assert resp.status == 401


async def test_pairing_when_no_code_is_open_is_refused(client: TestClient) -> None:
    resp = await client.post("/pair", json={"code": "ANYTHING"})
    assert resp.status == 401


async def test_every_pairing_failure_looks_the_same(client: TestClient, gateway: Gateway) -> None:
    """Wrong, expired, already used, never started. Telling them apart tells
    an attacker which half of the problem to work on."""
    messages = set()
    resp = await client.post("/pair", json={"code": "NOTOPEN1"})
    messages.add((resp.status, (await resp.json())["error"]))

    gateway.pairing.start()
    resp = await client.post("/pair", json={"code": "WRONGONE"})
    messages.add((resp.status, (await resp.json())["error"]))

    code = gateway.pairing.start()
    await client.post("/pair", json={"code": code})
    resp = await client.post("/pair", json={"code": code})  # reuse
    messages.add((resp.status, (await resp.json())["error"]))

    assert len(messages) == 1


def test_a_code_is_single_use() -> None:
    pairing = Pairing()
    code = pairing.start()
    assert pairing.redeem(code) is True
    assert pairing.redeem(code) is False


def test_a_code_expires() -> None:
    pairing = Pairing(ttl_s=-1)
    code = pairing.start()
    assert pairing.redeem(code) is False


def test_guessing_burns_the_code() -> None:
    """A code is one exchange. Anything that looks like a search means it is
    already compromised, so the code dies rather than the attempt count
    merely being noted."""
    pairing = Pairing()
    code = pairing.start()
    for _ in range(MAX_CODE_ATTEMPTS + 1):
        pairing.redeem("XXXXXXXX")
    assert pairing.redeem(code) is False


def test_codes_avoid_characters_that_get_misread() -> None:
    """It is read off one screen and typed into another."""
    assert not (set("O0I1") & set(CODE_ALPHABET))


def test_a_code_is_accepted_however_it_was_typed() -> None:
    pairing = Pairing()
    code = pairing.start()
    assert pairing.redeem(f"  {code[:4].lower()}-{code[4:].lower()} ") is True


# --- tokens ------------------------------------------------------------------

def test_only_the_hash_is_ever_written_to_disk(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    token, _device = store.issue("laptop")

    raw = (tmp_path / "tokens.json").read_text()
    assert token not in raw
    assert hash_token(token) in raw


def test_the_token_file_is_not_readable_by_anyone_else(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "remote" / "tokens.json")
    store.issue("laptop")
    path = tmp_path / "remote" / "tokens.json"
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0


def test_a_token_verifies_and_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    token, device = TokenStore(path).issue("phone")

    reopened = TokenStore(path)
    assert reopened.verify(token).id == device.id
    assert reopened.verify("not-the-token") is None


def test_revoking_a_device_stops_its_token_working(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    token, device = store.issue("phone")
    assert store.revoke(device.id) is True
    assert store.verify(token) is None
    assert store.revoke(device.id) is False


def test_one_corrupt_row_does_not_lock_everyone_out(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    token, _ = store.issue("good")
    path.write_text(path.read_text().replace('"devices": [', '"devices": [{"junk": true}, '))

    assert TokenStore(path).verify(token) is not None


async def test_a_paired_token_opens_the_authenticated_routes(client: TestClient, gateway: Gateway) -> None:
    code = gateway.pairing.start()
    token = (await (await client.post("/pair", json={"code": code})).json())["token"]

    resp = await client.get("/workspaces", headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 200


async def test_a_revoked_token_stops_working(client: TestClient, gateway: Gateway) -> None:
    code = gateway.pairing.start()
    body = await (await client.post("/pair", json={"code": code})).json()
    headers = {"Authorization": f"Bearer {body['token']}"}
    assert (await client.get("/workspaces", headers=headers)).status == 200

    await client.delete(f"/devices/{body['device']['id']}", headers=headers)
    assert (await client.get("/workspaces", headers=headers)).status == 401


# --- tickets -----------------------------------------------------------------

def test_a_ticket_is_single_use() -> None:
    tickets = Tickets()
    ticket = tickets.issue("device-1")
    assert tickets.redeem(ticket) == "device-1"
    assert tickets.redeem(ticket) is None


def test_a_ticket_expires() -> None:
    tickets = Tickets(ttl_s=-1)
    assert tickets.redeem(tickets.issue("device-1")) is None


async def test_the_socket_refuses_a_missing_or_bad_ticket(client: TestClient) -> None:
    for query in ["", "?ticket=made-up"]:
        resp = await client.get(f"/ws{query}")
        assert resp.status == 401


async def test_the_socket_will_not_take_the_durable_token(client: TestClient, gateway: Gateway) -> None:
    """The reason tickets exist: a token in a URL ends up in proxy logs and
    browser history, and it does not expire."""
    code = gateway.pairing.start()
    token = (await (await client.post("/pair", json={"code": code})).json())["token"]

    resp = await client.get(f"/ws?ticket={token}")
    assert resp.status == 401


async def test_a_ticket_opens_the_socket(client: TestClient, gateway: Gateway) -> None:
    code = gateway.pairing.start()
    token = (await (await client.post("/pair", json={"code": code})).json())["token"]
    headers = {"Authorization": f"Bearer {token}"}
    ticket = (await (await client.post("/ticket", headers=headers)).json())["ticket"]

    async with client.ws_connect(f"/ws?ticket={ticket}") as ws:
        await ws.send_json({"op": "ping", "op_id": "p"})
        ack = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert ack["ok"] is True


# --- tailscale identity ------------------------------------------------------

def test_tailscale_identity_is_only_trusted_on_a_loopback_bind(tmp_path: Path) -> None:
    """Tailscale's own docs are explicit: Serve adds these headers, and a
    backend reachable any other way lets a caller set them themselves. The
    unsafe combination must not be expressible at runtime, not merely
    discouraged."""
    loopback = Gateway(state_dir=tmp_path / "a", bind_host="127.0.0.1", tailscale=True)
    exposed = Gateway(state_dir=tmp_path / "b", bind_host="0.0.0.0", tailscale=True)

    assert loopback.trust_tailscale_identity is True
    assert exposed.trust_tailscale_identity is False


async def test_a_tailscale_login_is_accepted_when_bound_to_loopback(client: TestClient) -> None:
    resp = await client.get("/workspaces", headers={"Tailscale-User-Login": "me@example.com"})
    assert resp.status == 200


async def test_a_forged_identity_header_is_ignored_when_exposed(tmp_path: Path) -> None:
    gateway = Gateway(state_dir=tmp_path / "remote", bind_host="0.0.0.0", tailscale=True,
                      run_root=tmp_path / "run")
    async with TestClient(TestServer(create_gateway_app(gateway))) as client:
        resp = await client.get("/workspaces", headers={"Tailscale-User-Login": "me@example.com"})
        assert resp.status == 401


async def test_logins_can_be_restricted_to_a_list(tmp_path: Path) -> None:
    gateway = Gateway(state_dir=tmp_path / "remote", allowed_logins={"me@example.com"},
                      run_root=tmp_path / "run")
    async with TestClient(TestServer(create_gateway_app(gateway))) as client:
        ok = await client.get("/workspaces", headers={"Tailscale-User-Login": "me@example.com"})
        no = await client.get("/workspaces", headers={"Tailscale-User-Login": "someone@else.com"})
        assert (ok.status, no.status) == (200, 401)
