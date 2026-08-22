"""`GET /bus/ws` — watch any number of sessions over one socket.

The NDJSON body of `POST /task` answers "run this and tell me what happens".
This answers the different question a second client asks: "what is happening
right now, and what did I miss?" It is read-only in this form — starting turns
and answering questions still go through the existing POST routes.

No authentication, deliberately, and none is coming: this server binds
127.0.0.1 and trusts every caller, exactly as its other routes do. The thing
that faces the network is the remote gateway, which holds the tokens and
connects here over loopback like any other local client. Keeping the auth
boundary in one process, outside this one, is what lets the agent server's
security posture stay exactly as it was.

Framing is one JSON object per WS text message:

    {"v":1,"kind":"event","session":"s","seq":42,"event":{...TaskEvent...}}
    {"v":1,"kind":"control","session":"s","control":"snapshot", ...}
    {"v":1,"kind":"ack","op_id":"o1","ok":true}

`kind` keeps transport concerns out of the TaskEvent contract, which is frozen
(`events.py`). A client folds `event` with the same code it uses for the
NDJSON stream and needs to know nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from aiohttp import WSMsgType, web

PROTOCOL_VERSION = 1

#: Sent by aiohttp itself; the client is expected to answer. Pings must not
#: count as activity — see `_idle_shutdown_loop`.
HEARTBEAT_S = 25.0

#: Events replayed to a client attaching with no usable cursor.
SNAPSHOT_LIMIT = 800


def _frame(kind: str, **fields: Any) -> dict:
    return {"v": PROTOCOL_VERSION, "kind": kind, **fields}


class _Attachment:
    """One session this socket is watching, and the task pumping it."""

    __slots__ = ("session_id", "sub", "pump")

    def __init__(self, session_id: str, sub: Any, pump: asyncio.Task) -> None:
        self.session_id = session_id
        self.sub = sub
        self.pump = pump


def make_bus_ws_handler(app: web.Application):
    """Build the `/bus/ws` handler bound to this app's bus and event log."""

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=HEARTBEAT_S)
        await ws.prepare(request)

        bus = app["bus"]
        eventlog = app["eventlog"]
        reg = app["turn_registry"]
        attachments: dict[str, _Attachment] = {}
        # One writer per socket: aiohttp forbids concurrent sends, and several
        # attached sessions each have their own pump.
        send_lock = asyncio.Lock()

        async def send(frame: dict) -> None:
            async with send_lock:
                with contextlib.suppress(ConnectionResetError, RuntimeError):
                    await ws.send_json(frame)

        def touch() -> None:
            """Attaching or detaching is real activity; a heartbeat is not."""
            reg["last_active_at"] = time.monotonic()

        async def pump(session_id: str, sub: Any) -> None:
            while True:
                item = await sub.queue.get()
                if item is None:
                    return
                seq, event = item
                if sub.lagged:
                    # We dropped something to keep up. Splicing the rest onto a
                    # transcript with a hole in it would be quietly wrong, so
                    # say so and let the client start clean.
                    sub.lagged = False
                    await send(_frame("control", control="resync", session=session_id))
                await send(_frame("event", session=session_id, seq=seq, event=event))

        async def do_attach(op: dict) -> dict:
            session_id = str(op.get("session") or "")
            if not session_id:
                return {"ok": False, "error": "session is required"}
            if session_id in attachments:
                return {"ok": False, "error": "already attached"}

            since = op.get("since")
            since = int(since) if isinstance(since, int) else None
            wants_ask = bool(op.get("wants_ask"))

            channel = bus.channel(session_id)
            events, cursor, truncated = channel.backlog(since=since)
            if since is None or truncated:
                # The ring cannot join up with what this client holds, so give
                # it the recorded history instead and tell it to start clean.
                # Flush first: the newest events are still queued in memory.
                eventlog.flush()
                events = eventlog.replay(session_id, limit=SNAPSHOT_LIMIT)
                truncated = True

            # Subscribe before sending the snapshot, so an event published in
            # between is queued rather than lost — the pump starts after, and
            # `seq` lets the client drop anything it already has.
            sub = bus.subscribe(session_id, wants_ask=wants_ask)
            await send(_frame(
                "control",
                control="snapshot",
                session=session_id,
                seq=cursor,
                truncated=truncated,
                busy=reg["sessions"].get(session_id, 0) > 0,
                pending_ask=channel.pending_ask,
                todos=channel.todos,
                events=events,
            ))
            attachments[session_id] = _Attachment(
                session_id, sub, asyncio.create_task(pump(session_id, sub))
            )
            touch()
            return {"ok": True, "seq": cursor}

        async def do_detach(op: dict) -> dict:
            session_id = str(op.get("session") or "")
            att = attachments.pop(session_id, None)
            if att is None:
                return {"ok": False, "error": "not attached"}
            await _drop(att)
            touch()
            return {"ok": True}

        async def _drop(att: _Attachment) -> None:
            bus.unsubscribe(att.session_id, att.sub)  # closes the queue
            att.pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await att.pump

        async def do_sessions(_op: dict) -> dict:
            """The session directory a client renders as a list. Unions the
            recorded sessions with whatever is live in this process, so a
            session that has not written to the log yet still shows up."""
            eventlog.flush()
            rows = {r["session_id"]: dict(r) for r in eventlog.sessions()}
            for session_id in bus.sessions():
                rows.setdefault(session_id, {"session_id": session_id, "title": None})
            for session_id, row in rows.items():
                row["busy"] = reg["sessions"].get(session_id, 0) > 0
                channel = bus.peek(session_id)
                row["pending_ask"] = bool(channel and channel.pending_ask)
            return {"ok": True, "sessions": list(rows.values())}

        ops = {
            "attach": do_attach,
            "detach": do_detach,
            "sessions": do_sessions,
            "ping": lambda _op: asyncio.sleep(0, result={"ok": True}),
        }

        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                try:
                    op = msg.json()
                except ValueError:
                    await send(_frame("ack", ok=False, error="invalid json"))
                    continue
                handler_fn = ops.get(str(op.get("op") or ""))
                op_id = op.get("op_id")
                if handler_fn is None:
                    result = {"ok": False, "error": f"unknown op {op.get('op')!r}"}
                else:
                    try:
                        result = await handler_fn(op)
                    except Exception as exc:  # one bad op must not drop the socket
                        result = {"ok": False, "error": str(exc)}
                await send(_frame("ack", op_id=op_id, **result))
        finally:
            for att in list(attachments.values()):
                await _drop(att)
            attachments.clear()
            touch()
        return ws

    return handler
