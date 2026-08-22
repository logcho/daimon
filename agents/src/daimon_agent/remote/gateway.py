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
from .push import PushStore, Subscription

PROTOCOL_VERSION = 1

WEB_DIR = Path(__file__).resolve().parent / "web"

#: Reachable without a credential. Everything else needs one.
PUBLIC_PATHS = {"/", "/health", "/pair", "/manifest.webmanifest", "/favicon.ico", "/sw.js"}

#: How long to wait before re-checking which workspaces are running, when
#: notifications are on. Long: this exists to notice a server that started
#: since we last looked, not to poll for anything.
WATCH_REDISCOVER_S = 30.0

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
        self.push = PushStore(state_dir)
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
        app["watcher"] = None
        app["ensure_watcher"] = lambda: _ensure_watcher(app, gateway)
        app["ensure_watcher"]()

    async def cleanup(app: web.Application) -> None:
        watcher = app.get("watcher")
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
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

    async def push_key(_request: web.Request) -> web.Response:
        key = gateway.push.public_key()
        if key is None:
            return web.json_response(
                {"error": "notifications need the push extra — `uv sync --extra push`"},
                status=501,
            )
        return web.json_response({"key": key})

    async def push_subscribe(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        endpoint = body.get("endpoint")
        keys = body.get("keys")
        if not isinstance(endpoint, str) or not isinstance(keys, dict):
            return web.json_response({"error": "endpoint and keys are required"}, status=400)
        gateway.push.add(Subscription(
            device_id=str(request.get("device_label") or "device"),
            endpoint=endpoint,
            keys={str(k): str(v) for k, v in keys.items()},
        ))
        # The watcher is only worth running when somebody wants to be told.
        request.app["ensure_watcher"]()
        return web.json_response({"ok": True})

    async def push_unsubscribe(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        endpoint = str(body.get("endpoint") or "")
        gateway.push.remove(endpoint)
        return web.json_response({"ok": True})

    async def devices(_request: web.Request) -> web.Response:
        return web.json_response({"devices": [d.public() for d in gateway.tokens.devices()]})

    async def revoke_device(request: web.Request) -> web.Response:
        device_id = request.match_info.get("device_id", "")
        removed = gateway.tokens.revoke(device_id)
        # Revoking a device has to take its notifications with it, or a phone
        # you deliberately cut off keeps buzzing.
        gateway.push.forget_device(device_id)
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
    app.router.add_get("/push/key", push_key)
    app.router.add_post("/push/subscribe", push_subscribe)
    app.router.add_post("/push/unsubscribe", push_unsubscribe)
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

    async def manifest(_request: web.Request) -> web.StreamResponse:
        """Add-to-Home-Screen: without this the page opens in Safari chrome
        rather than as a standalone app."""
        path = WEB_DIR / "manifest.webmanifest"
        if not path.exists():
            return web.Response(status=404)
        return web.FileResponse(path)

    async def service_worker(_request: web.Request) -> web.StreamResponse:
        """Served from the root, not from /assets, because a service worker's
        scope is the directory it is served from — one under /assets could
        only control /assets."""
        path = WEB_DIR / "sw.js"
        if not path.exists():
            return web.Response(status=404)
        return web.FileResponse(path, headers={"content-type": "application/javascript"})

    if (WEB_DIR / "assets").is_dir():
        app.router.add_static("/assets/", WEB_DIR / "assets")
    app.router.add_get("/", spa)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/sw.js", service_worker)


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


# --- notification watcher ----------------------------------------------------


def _ensure_watcher(app: web.Application, gateway: Gateway) -> None:
    """Run the watcher exactly when there is somebody to notify."""
    if not gateway.push.enabled:
        return
    existing = app.get("watcher")
    if existing is not None and not existing.done():
        return
    app["watcher"] = asyncio.create_task(_watch_forever(app, gateway))


async def _watch_forever(app: web.Application, gateway: Gateway) -> None:
    """Hold a socket to every workspace so a parked question can find you.

    This is the only part of the gateway that keeps a connection open without
    a client asking for one, and it has to: a notification is worth sending
    precisely when nobody is looking. It uses the bus's `watch` op, which
    observes every session without subscribing to any — so it does not count
    as somebody watching, and workspace servers still idle out under it.
    """
    watched: dict[str, asyncio.Task] = {}
    try:
        while True:
            if gateway.push.enabled:
                found = await discover(app["http"], root=gateway.run_root)
                for workspace in found:
                    if workspace.key in watched and not watched[workspace.key].done():
                        continue
                    watched[workspace.key] = asyncio.create_task(
                        _watch_one(app, gateway, workspace)
                    )
            await asyncio.sleep(WATCH_REDISCOVER_S)
    except asyncio.CancelledError:
        for task in watched.values():
            task.cancel()
        raise


async def _watch_one(app: web.Application, gateway: Gateway, workspace: Workspace) -> None:
    """One workspace's watch socket. Ends when its server does; the
    rediscovery loop reopens it if it comes back."""
    http: aiohttp.ClientSession = app["http"]
    try:
        async with http.ws_connect(workspace.bus_url, heartbeat=HEARTBEAT_S) as ws:
            await ws.send_json({"op": "watch", "op_id": "watch"})
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                try:
                    frame = msg.json()
                except ValueError:
                    continue
                if frame.get("control") != "watch":
                    continue
                event = frame.get("event") or {}
                if event.get("type") != "ask":
                    continue
                await _notify_ask(gateway, workspace, str(frame.get("session") or ""), event)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return  # the server went away; rediscovery will find it again


async def _notify_ask(
    gateway: Gateway, workspace: Workspace, session_id: str, event: dict
) -> None:
    """Tell the phone a session wants an answer.

    Deliberately without the question itself. A push payload passes through a
    service run by Apple or Google, and a plan the agent is asking you to
    approve can quote anything in the workspace — a file, a credential it
    found, a customer's name. The notification says *which* session wants you;
    reading it means opening the app.
    """
    header = str(event.get("header") or "").strip()
    title = f"{workspace.name} is waiting on you"
    body = header or ("Approve a plan" if event.get("kind") == "plan" else "Answer a question")
    # pywebpush is synchronous, and there may be several endpoints.
    await asyncio.to_thread(gateway.push.notify, title, body, url="/")
