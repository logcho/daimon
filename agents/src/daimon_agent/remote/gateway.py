"""The gateway: one authenticated front door for every agent server here.

Why a separate process rather than routes on the agent server:

- `tailscale serve` maps one hostname to one *fixed* local port, and agent
  servers take a freshly allocated port every spawn. Something stable has to
  be in front of them.
- There is more than one of them — one per workspace, plus the desktop app's —
  and a phone wants to see all of them, not to be told which port to guess.
- Most importantly, the agent servers keep no network listener and no auth
  code at all. `POST /task` and the terminal service are arbitrary code
  execution on this machine; the strongest guarantee available is that they
  are not reachable from the network *by construction*, and that guarantee
  survives any future routing mistake made in this file.

The gateway itself binds loopback in its default mode, and `tailscale serve`
proxies to it. That is what makes the `Tailscale-User-Login` header
trustworthy: Tailscale's own documentation is explicit that a backend
reachable any other way lets a caller set that header themselves. The bind is
asserted before the header is ever believed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

from .auth import Pairing, TokenStore, Tickets
from .discovery import Workspace, discover

PROTOCOL_VERSION = 1

WEB_DIR = Path(__file__).resolve().parent / "web"

#: Reachable without a credential. Everything else needs one.
PUBLIC_PATHS = {"/", "/health", "/pair", "/manifest.webmanifest", "/favicon.ico"}

#: Ops the gateway answers itself; everything else is forwarded to a workspace.
LOCAL_OPS = {"workspaces", "ping"}

HEARTBEAT_S = 25.0


def _frame(kind: str, **fields: Any) -> dict:
    return {"v": PROTOCOL_VERSION, "kind": kind, **fields}


def default_state_dir() -> Path:
    return Path.home() / ".local" / "share" / "daimon" / "remote"


class Gateway:
    """Everything the gateway knows, so the handlers stay thin."""

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        bind_host: str = "127.0.0.1",
        tailscale: bool = True,
        allowed_logins: set[str] | None = None,
        run_root: Path | None = None,
        allow_terminals: bool = False,
    ) -> None:
        state_dir = state_dir or default_state_dir()
        self.tokens = TokenStore(state_dir / "tokens.json")
        self.pairing = Pairing()
        self.tickets = Tickets()
        self.bind_host = bind_host
        self.run_root = run_root
        # Trusting an identity header is a property of the bind, not of a
        # setting: if we are reachable anywhere but loopback, anyone can send
        # that header themselves. Refusing here rather than at request time
        # means the unsafe combination cannot exist at runtime at all.
        self.trust_tailscale_identity = tailscale and bind_host in ("127.0.0.1", "::1", "localhost")
        self.tailscale_requested = tailscale
        self.allowed_logins = allowed_logins or set()
        # A shell runs outside the workspace confinement every other execution
        # surface here respects — that is what a terminal is for, and it is why
        # handing one to a remote device is a separate, explicit decision
        # rather than something that comes along with pairing.
        self.allow_terminals = allow_terminals

    def identify(self, request: web.Request) -> tuple[bool, str | None]:
        """Who is calling, as `(allowed, label)`."""
        if self.trust_tailscale_identity:
            login = request.headers.get("Tailscale-User-Login")
            if login and (not self.allowed_logins or login in self.allowed_logins):
                return True, login
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            device = self.tokens.verify(header[7:].strip())
            if device is not None:
                self.tokens.touch()
                return True, device.label
        return False, None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    gateway: Gateway = request.app["gateway"]
    path = request.path
    if path in PUBLIC_PATHS or path.startswith("/assets/"):
        return await handler(request)
    if path == "/ws":
        return await handler(request)  # gated by its ticket, checked in the handler
    allowed, label = gateway.identify(request)
    if not allowed:
        return web.json_response({"error": "unauthorized"}, status=401)
    request["device_label"] = label
    return await handler(request)


def create_gateway_app(gateway: Gateway) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["gateway"] = gateway

    async def startup(app: web.Application) -> None:
        app["http"] = aiohttp.ClientSession()

    async def cleanup(app: web.Application) -> None:
        await app["http"].close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)

    # --- unauthenticated -----------------------------------------------------

    async def health(_request: web.Request) -> web.Response:
        """Deliberately says nothing a stranger could use — no workspace paths,
        no session names, no device labels. Just enough to tell a client it has
        reached a daimon gateway and whether pairing is open."""
        return web.json_response({
            "ok": True,
            "service": "daimon-remote",
            "version": PROTOCOL_VERSION,
            "pairing": gateway.pairing.pending,
        })

    async def pair(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        code = str(body.get("code") or "")
        label = str(body.get("label") or "device")
        if not gateway.pairing.redeem(code):
            # One message for every failure mode — wrong, expired, already
            # used, or never started. Distinguishing them tells an attacker
            # which half of the problem to work on.
            return web.json_response({"error": "pairing failed"}, status=401)
        token, device = gateway.tokens.issue(label)
        return web.json_response({"token": token, "device": device.public()})

    # --- authenticated -------------------------------------------------------

    async def ticket(request: web.Request) -> web.Response:
        allowed, label = gateway.identify(request)
        if not allowed:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({
            "ticket": gateway.tickets.issue(label or "device"),
            "expires_in": gateway.tickets.ttl_s,
        })

    async def workspaces(request: web.Request) -> web.Response:
        found = await discover(request.app["http"], root=gateway.run_root)
        return web.json_response({"workspaces": [_workspace_public(w) for w in found]})

    async def devices(_request: web.Request) -> web.Response:
        return web.json_response({"devices": [d.public() for d in gateway.tokens.devices()]})

    async def revoke_device(request: web.Request) -> web.Response:
        device_id = request.match_info.get("device_id", "")
        removed = gateway.tokens.revoke(device_id)
        # Live sockets belonging to a revoked device are closed by the ws
        # handler, which re-checks on every op — revocation that leaves an
        # open socket working is not revocation.
        return web.json_response({"ok": removed}, status=200 if removed else 404)

    # --- the multiplexed socket ---------------------------------------------

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        # Browsers cannot set headers on a WS handshake, so the credential is a
        # single-use ticket in the query string — worthless within seconds and
        # never the durable token. See auth.py.
        device_id = gateway.tickets.redeem(request.query.get("ticket", ""))
        if device_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        origin = request.headers.get("Origin")
        if origin and not _origin_ok(origin, request):
            return web.json_response({"error": "bad origin"}, status=403)
        return await _serve_socket(request, gateway)

    app.router.add_get("/health", health)
    app.router.add_post("/pair", pair)
    app.router.add_post("/ticket", ticket)
    app.router.add_get("/workspaces", workspaces)
    app.router.add_get("/devices", devices)
    app.router.add_delete("/devices/{device_id}", revoke_device)
    app.router.add_get("/ws", websocket)
    _add_static_routes(app)
    return app


def _workspace_public(w: Workspace) -> dict:
    return {"key": w.key, "name": w.name, "path": w.path, "port": w.port}


def _origin_ok(origin: str, request: web.Request) -> bool:
    """Belt and braces on top of the ticket. A browser will happily open a
    WebSocket cross-origin, and while a cross-site page cannot obtain a ticket
    (that needs an Authorization header, which needs a preflight we refuse),
    checking the host it claims costs nothing."""
    host = request.headers.get("Host", "")
    return origin.split("://")[-1] == host


def _add_static_routes(app: web.Application) -> None:
    """Serve the mobile client, if it has been built."""
    index = WEB_DIR / "index.html"

    async def spa(_request: web.Request) -> web.StreamResponse:
        if not index.exists():
            return web.Response(
                status=503,
                text="the mobile client has not been built — run `npm run build:mobile` in app/",
            )
        return web.FileResponse(index)

    if (WEB_DIR / "assets").is_dir():
        app.router.add_static("/assets/", WEB_DIR / "assets")
    app.router.add_get("/", spa)


# --- socket plumbing ---------------------------------------------------------


class _Upstream:
    """One connection to one workspace's `/bus/ws`."""

    def __init__(self, key: str, ws: aiohttp.ClientWebSocketResponse, pump: asyncio.Task) -> None:
        self.key = key
        self.ws = ws
        self.pump = pump

    async def close(self) -> None:
        self.pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.pump
        with contextlib.suppress(Exception):
            await self.ws.close()


async def _serve_socket(request: web.Request, gateway: Gateway) -> web.WebSocketResponse:
    """Relay between one phone and the agent servers it asks for.

    The gateway holds no session or terminal state of its own — it opens a bus
    socket per workspace on demand and forwards. That is what lets it be
    restarted freely: everything replayable lives in the workspace servers.
    """
    ws = web.WebSocketResponse(heartbeat=HEARTBEAT_S)
    await ws.prepare(request)
    http: aiohttp.ClientSession = request.app["http"]
    upstreams: dict[str, _Upstream] = {}
    send_lock = asyncio.Lock()

    async def send(frame: dict) -> None:
        async with send_lock:
            with contextlib.suppress(ConnectionResetError, RuntimeError):
                await ws.send_json(frame)

    async def pump(key: str, upstream_ws: aiohttp.ClientWebSocketResponse) -> None:
        """Relay a workspace's frames, tagged so the client can tell them
        apart. Acks pass through untouched: the client made the op_id, so it
        can match the answer itself."""
        async for msg in upstream_ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                frame = msg.json()
            except ValueError:
                continue
            if frame.get("kind") != "ack":
                frame["workspace"] = key
            await send(frame)
        # The workspace server went away — restarted, or idled out. Say so
        # rather than leaving the client attached to nothing.
        upstreams.pop(key, None)
        await send(_frame("control", control="workspace_lost", workspace=key))

    async def upstream_for(key: str) -> _Upstream | None:
        existing = upstreams.get(key)
        if existing is not None:
            return existing
        found = await discover(http, root=gateway.run_root)
        target = next((w for w in found if w.key == key), None)
        if target is None:
            return None
        try:
            upstream_ws = await http.ws_connect(target.bus_url, heartbeat=HEARTBEAT_S)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        upstream = _Upstream(key, upstream_ws, asyncio.create_task(pump(key, upstream_ws)))
        upstreams[key] = upstream
        return upstream

    try:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                op = msg.json()
            except ValueError:
                await send(_frame("ack", ok=False, error="invalid json"))
                continue
            name = str(op.get("op") or "")
            op_id = op.get("op_id")

            if name == "ping":
                await send(_frame("ack", op_id=op_id, ok=True))
                continue
            if name == "workspaces":
                found = await discover(http, root=gateway.run_root)
                await send(_frame("ack", op_id=op_id, ok=True,
                                  workspaces=[_workspace_public(w) for w in found]))
                continue

            if name.startswith("term.") and not gateway.allow_terminals:
                await send(_frame("ack", op_id=op_id, ok=False,
                                  error="terminals are not exposed — restart the "
                                        "gateway with --terminals"))
                continue

            key = str(op.get("workspace") or "")
            if not key:
                await send(_frame("ack", op_id=op_id, ok=False,
                                  error="workspace is required"))
                continue
            upstream = await upstream_for(key)
            if upstream is None:
                await send(_frame("ack", op_id=op_id, ok=False,
                                  error="workspace is not running"))
                continue
            forwarded = {k: v for k, v in op.items() if k != "workspace"}
            try:
                await upstream.ws.send_json(forwarded)
            except (ConnectionResetError, RuntimeError):
                await send(_frame("ack", op_id=op_id, ok=False, error="workspace went away"))
    finally:
        for upstream in list(upstreams.values()):
            await upstream.close()
        upstreams.clear()
        await ws.close()
    return ws
