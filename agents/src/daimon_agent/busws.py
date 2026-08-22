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
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .events import ask_resolved_event, user_event
from .run import resume_turn, run_turn
from .vault import is_internal, vault_file

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
        watching: Any = None  # unsubscribe for the all-sessions observer
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
                try:
                    await ws.send_json(frame)
                except (ConnectionResetError, RuntimeError):
                    return  # the client is gone; the finally block cleans up
                except TypeError as exc:
                    # An op returned something json cannot represent. Letting
                    # this kill the writer means the socket goes silent with no
                    # ack and no close — the client waits forever for an answer
                    # that was never going to arrive. Say so instead.
                    print(f"[daimon-agent] unserializable frame dropped: {exc}", file=sys.stderr)
                    with contextlib.suppress(Exception):
                        await ws.send_json(_frame(
                            "ack", op_id=frame.get("op_id"), ok=False,
                            error="the server produced a response it could not send",
                        ))

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

        # --- prompting ------------------------------------------------------

        async def do_prompt(op: dict) -> dict:
            """Start a turn. The result arrives as ordinary events on this
            socket, like any other turn on this session — there is no second
            connection holding the body, which is the difference between a
            client that *streams* a turn and one that *watches* it."""
            session_id = str(op.get("session") or "")
            instruction = op.get("instruction")
            if not session_id:
                return {"ok": False, "error": "session is required"}
            if not isinstance(instruction, str) or not instruction.strip():
                return {"ok": False, "error": "instruction is required"}
            mode = op.get("mode") if op.get("mode") in ("plan", "normal") else "normal"

            # Only offer the ask tools if this client said it can answer. An
            # agent that asks a question nobody will answer has hung, not
            # paused — the same rule POST /task applies.
            attachment = attachments.get(session_id)
            capabilities = ["ask"] if attachment and attachment.sub.wants_ask else []

            graph = await app["graph_for"](session_id)
            app["skills"] = app["discover_skills"]()

            async def run(emit) -> Any:
                emit(user_event(instruction, mode=mode, origin=str(op.get("origin") or "remote")))
                return await run_turn(
                    instruction, session_id, app["settings"], emit,
                    graph=graph, memory=app["memory"], skills=app["skills"],
                    router=app["router"], live_frames=app["frame_task"],
                    capabilities=capabilities, mode=mode,
                )

            # Detached: a turn can outlast this socket. A phone going into a
            # tunnel mid-turn must not cancel work the laptop is also watching.
            asyncio.ensure_future(app["run_turn_detached"](session_id, run))
            return {"ok": True}

        async def do_answer(op: dict) -> dict:
            """Answer a question the agent parked on. The turn resumes from
            exactly where it suspended — the checkpointer held the state, which
            is why this works hours later and from a different device than the
            one that saw the question."""
            session_id = str(op.get("session") or "")
            if not session_id or "answer" not in op:
                return {"ok": False, "error": "session and answer are required"}

            channel = app["bus"].channel(session_id)
            ask_id = op.get("ask_id")
            pending = channel.pending_ask
            if ask_id is not None and pending is not None and pending.get("id") != ask_id:
                return {"ok": False, "error": "already answered"}
            if ask_id is not None and pending is None and channel.answered_ask_id == ask_id:
                return {"ok": False, "error": "already answered"}
            claimed = channel.clear_pending_ask()
            if claimed is None:
                return {"ok": False, "error": "nothing is waiting on an answer"}

            by = str(op.get("by") or "") or None
            publish = app["bus"].publisher(session_id)
            publish(ask_resolved_event(str(claimed.get("id") or ""), op["answer"], by=by))

            graph = await app["graph_for"](session_id)

            async def run(emit) -> Any:
                return await resume_turn(
                    op["answer"], session_id, app["settings"], emit,
                    graph=graph, memory=app["memory"], router=app["router"],
                    live_frames=app["frame_task"],
                )

            asyncio.ensure_future(app["run_turn_detached"](session_id, run))
            return {"ok": True}

        async def do_interrupt(op: dict) -> dict:
            session_id = str(op.get("session") or "")
            if not session_id:
                return {"ok": False, "error": "session is required"}
            return await app["interrupt_session"](session_id, str(op.get("by") or "") or None)

        # --- watching everything ---------------------------------------------

        async def do_watch(_op: dict) -> dict:
            """Report noteworthy events on *every* session, without attaching
            to any of them.

            This exists for notifications, and it cannot be built out of
            `attach`: to attach you must already know the session id, and the
            whole question is "did anything, anywhere, start waiting on me?"
            Only asks and turn endings are forwarded — the point is to know
            something happened, not to mirror a transcript nobody is reading.

            The subscription is passive, so it does not count as somebody
            watching. Otherwise a gateway keeping this open so it can notify
            you would stop every workspace server on the machine from ever
            idling out.
            """
            nonlocal watching
            if watching is not None:
                return {"ok": True}  # idempotent

            def observe(session_id: str, seq: int, event: dict) -> None:
                etype = event.get("type")
                if etype not in ("ask", "ask_resolved", "done", "error"):
                    return
                send(_frame("control", control="watch", session=session_id,
                            seq=seq, event=event))

            watching = bus.observe(observe)
            return {"ok": True}

        # --- notes ----------------------------------------------------------
        #
        # Reads and writes go through `_vault_file`, the same resolve-then-check
        # containment the HTTP routes use, so a client-supplied name cannot walk
        # out of the vault with `..` or a symlink. Deliberately not gated behind
        # a flag the way terminals are: the vault is one confined directory of
        # markdown, not a shell on the host.

        async def do_vault_list(_op: dict) -> dict:
            root = Path(app["settings"].vault_dir)
            if not root.is_dir():
                return {"ok": True, "notes": []}
            notes = []
            for path in root.rglob("*.md"):
                if is_internal(path.relative_to(root)):
                    continue
                stat = path.stat()
                notes.append({
                    "name": str(path.relative_to(root)),
                    "sizeBytes": stat.st_size,
                    "modifiedAt": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                })
            notes.sort(key=lambda n: n["modifiedAt"], reverse=True)
            return {"ok": True, "notes": notes}

        async def do_vault_read(op: dict) -> dict:
            name = str(op.get("name") or "")
            try:
                path = vault_file(app["settings"], name)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if not path.is_file():
                return {"ok": False, "error": "no such note"}
            return {"ok": True, "name": name,
                    "content": path.read_text(encoding="utf-8", errors="replace")}

        async def do_vault_write(op: dict) -> dict:
            name = str(op.get("name") or "")
            content = op.get("content")
            if not isinstance(content, str):
                return {"ok": False, "error": "content must be a string"}
            try:
                path = vault_file(app["settings"], name)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            # Only markdown: the listing is rglob("*.md"), so anything else
            # would be written and then never shown again.
            if path.suffix.lower() != ".md":
                return {"ok": False, "error": "note names must end in .md"}
            if path.is_dir():
                return {"ok": False, "error": "that name is a directory"}
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            memory_store = app["memory"]
            if memory_store is not None:
                # Best-effort, as the HTTP path is: a failed reindex costs
                # recall accuracy, not the user's note.
                with contextlib.suppress(Exception):
                    memory_store.index_note(name, content)
            return {"ok": True, "name": name}

        # --- skills and config (read-mostly) ----------------------------------

        async def do_skills(_op: dict) -> dict:
            # `discover_skills` returns dataclasses, and the body of every
            # skill with them — neither of which belongs on the wire. A client
            # renders name, description and which library it came from; the
            # body is what `skill.read` is for.
            app["skills"] = app["discover_skills"]()
            return {"ok": True, "skills": [
                {"name": s.name, "description": s.description, "source": s.source}
                for s in app["skills"]
            ]}

        async def do_skill_read(op: dict) -> dict:
            name = str(op.get("name") or "")
            for skill in app["discover_skills"]():
                if skill.name == name:
                    return {"ok": True, "name": name, "source": skill.source,
                            "content": skill.content}
            return {"ok": False, "error": "no such skill"}

        async def do_config(_op: dict) -> dict:
            """What the agent is configured with — never the keys themselves.

            The HTTP route returns booleans rather than values for exactly this
            reason, and going out over a network does not make that looser.
            """
            settings = app["settings"]
            return {"ok": True, "config": {
                "model": settings.model,
                "flash_model": settings.flash_model,
                "provider": settings.provider,
                "workspace": str(settings.resolved_workspace_dir),
                "vault": str(settings.vault_dir),
                "api_key_configured": bool(settings.api_key or settings.anthropic_api_key),
            }}

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
            "prompt": do_prompt,
            "answer": do_answer,
            "interrupt": do_interrupt,
            "watch": do_watch,
            "skills": do_skills,
            "skill.read": do_skill_read,
            "config": do_config,
            "vault.list": do_vault_list,
            "vault.read": do_vault_read,
            "vault.write": do_vault_write,
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
            if watching is not None:
                watching()
                watching = None
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
