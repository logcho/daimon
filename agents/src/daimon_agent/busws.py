"""`GET /bus/ws` — watch sessions and drive terminals over one socket.

The NDJSON body of `POST /task` answers "run this and tell me what happens".
This answers the different question a second client asks: "what is happening
right now, what did I miss, and let me type into that shell."

No authentication, deliberately, and none is coming: this server binds
127.0.0.1 and trusts every caller, exactly as its other routes do. The thing
that faces the network is the remote gateway, which holds the tokens and
connects here over loopback like any other local client. Keeping the auth
boundary in one process, outside this one, is what lets the agent server's
security posture stay exactly as it was.

Framing is one JSON object per WS text message:

    {"v":1,"kind":"event","session":"s","seq":42,"event":{...TaskEvent...}}
    {"v":1,"kind":"term","id":"t1","data":"<base64>"}
    {"v":1,"kind":"control","control":"snapshot", ...}
    {"v":1,"kind":"ack","op_id":"o1","ok":true}

`kind` keeps transport concerns out of the TaskEvent contract, which is frozen
(`events.py`). A client folds `event` with the same code it uses for the
NDJSON stream and needs to know nothing else. Terminal payloads are base64
because they are arbitrary bytes, not text — the same choice the Tauri
`terminal-output` event made.
"""

from __future__ import annotations

import asyncio
import base64
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

#: Frames allowed to queue for one socket. Terminal output cannot be dropped
#: selectively — a hole in a byte stream corrupts the emulator's state — so a
#: client that falls this far behind gets disconnected instead, and re-attaches
#: to a fresh snapshot. Self-healing beats a silently wrong screen.
OUTBOX_LIMIT = 4096


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
    """Build the `/bus/ws` handler bound to this app's bus, log and terminals."""

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=HEARTBEAT_S)
        await ws.prepare(request)

        bus = app["bus"]
        eventlog = app["eventlog"]
        terminals = app["terminals"]
        reg = app["turn_registry"]
        attachments: dict[str, _Attachment] = {}
        watched_terminals: set[str] = set()
        # One outbound queue and one writer: aiohttp forbids concurrent sends,
        # and session pumps and terminal callbacks both produce frames. The
        # queue is also what lets a synchronous PTY read callback hand work to
        # the socket without awaiting.
        outbox: asyncio.Queue = asyncio.Queue()
        overflowed = asyncio.Event()

        def send(frame: dict) -> None:
            """Queue a frame. Synchronous, so a PTY callback can call it."""
            if outbox.qsize() >= OUTBOX_LIMIT:
                overflowed.set()
                return
            outbox.put_nowait(frame)

        async def writer() -> None:
            while True:
                frame = await outbox.get()
                if frame is None:
                    return
                with contextlib.suppress(ConnectionResetError, RuntimeError):
                    await ws.send_json(frame)

        async def flush(frame: dict) -> None:
            """Queue a frame and wait for the writer to drain — used for acks,
            so a caller's next op cannot overtake the answer to this one."""
            send(frame)
            while not outbox.empty():
                await asyncio.sleep(0)

        writer_task = asyncio.create_task(writer())

        def touch() -> None:
            """Attaching or detaching is real activity; a heartbeat is not."""
            reg["last_active_at"] = time.monotonic()

        # --- sessions -------------------------------------------------------

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
                    send(_frame("control", control="resync", session=session_id))
                send(_frame("event", session=session_id, seq=seq, event=event))

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
            send(_frame(
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
            att = attachments.pop(str(op.get("session") or ""), None)
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

        # --- terminals ------------------------------------------------------

        def on_terminal(kind: str, term_id: str, payload: Any) -> None:
            """Called synchronously from the PTY reader. Queues, never awaits."""
            if kind == "output":
                send(_frame("term", id=term_id, data=base64.b64encode(payload).decode()))
            else:
                send(_frame("control", control="term_exited", id=term_id, code=payload))

        async def do_term_open(op: dict) -> dict:
            term = await terminals.open(
                term_id=str(op["id"]) if op.get("id") else None,
                cwd=str(op["cwd"]) if op.get("cwd") else None,
                cols=int(op.get("cols") or 80),
                rows=int(op.get("rows") or 24),
            )
            touch()
            return {"ok": True, "terminal": term.describe()}

        async def do_term_list(_op: dict) -> dict:
            return {"ok": True, "terminals": terminals.list()}

        async def do_term_attach(op: dict) -> dict:
            term = terminals.get(str(op.get("id") or ""))
            if term is None:
                return {"ok": False, "error": "no such terminal"}
            cols = int(op["cols"]) if op.get("cols") else None
            rows = int(op["rows"]) if op.get("rows") else None
            replay = term.attach(on_terminal, cols=cols, rows=rows)
            watched_terminals.add(term.id)
            # The scrollback rides the ack's snapshot rather than the output
            # stream, so a client can tell "catch up" from "this just happened"
            # and clear its emulator before writing it.
            send(_frame(
                "control",
                control="term_snapshot",
                id=term.id,
                data=base64.b64encode(replay).decode(),
                cols=term.cols,
                rows=term.rows,
                exited=term.exited,
            ))
            touch()
            return {"ok": True, "terminal": term.describe()}

        async def do_term_detach(op: dict) -> dict:
            term = terminals.get(str(op.get("id") or ""))
            watched_terminals.discard(str(op.get("id") or ""))
            if term is not None:
                term.detach(on_terminal)
            touch()
            return {"ok": True}

        async def do_term_input(op: dict) -> dict:
            term = terminals.get(str(op.get("id") or ""))
            if term is None:
                return {"ok": False, "error": "no such terminal"}
            raw = op.get("data")
            if not isinstance(raw, str):
                return {"ok": False, "error": "data is required"}
            # base64 so a client can send arbitrary key sequences; plain text is
            # accepted too because most input is exactly that.
            data = base64.b64decode(raw) if op.get("encoding") == "base64" else raw.encode()
            term.write(data)
            return {"ok": True}

        async def do_term_resize(op: dict) -> dict:
            term = terminals.get(str(op.get("id") or ""))
            if term is None:
                return {"ok": True}  # a resize racing the spawn is not an error
            term.resize(on_terminal, int(op.get("cols") or 80), int(op.get("rows") or 24))
            return {"ok": True, "cols": term.cols, "rows": term.rows}

        async def do_term_close(op: dict) -> dict:
            term_id = str(op.get("id") or "")
            watched_terminals.discard(term_id)
            return {"ok": True, "closed": terminals.close(term_id)}

        ops = {
            "attach": do_attach,
            "detach": do_detach,
            "sessions": do_sessions,
            "term.open": do_term_open,
            "term.list": do_term_list,
            "term.attach": do_term_attach,
            "term.detach": do_term_detach,
            "term.input": do_term_input,
            "term.resize": do_term_resize,
            "term.close": do_term_close,
            "ping": lambda _op: asyncio.sleep(0, result={"ok": True}),
        }

        async def read_ops() -> None:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                try:
                    op = msg.json()
                except ValueError:
                    await flush(_frame("ack", ok=False, error="invalid json"))
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
                await flush(_frame("ack", op_id=op_id, **result))

        try:
            reader = asyncio.create_task(read_ops())
            spilled = asyncio.create_task(overflowed.wait())
            await asyncio.wait({reader, spilled}, return_when=asyncio.FIRST_COMPLETED)
            if not reader.done():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            spilled.cancel()
        finally:
            for att in list(attachments.values()):
                await _drop(att)
            attachments.clear()
            for term_id in watched_terminals:
                term = terminals.get(term_id)
                if term is not None:
                    term.detach(on_terminal)
            watched_terminals.clear()
            outbox.put_nowait(None)
            with contextlib.suppress(asyncio.CancelledError):
                await writer_task
            touch()
            await ws.close()
        return ws

    return handler
