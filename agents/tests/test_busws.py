"""`GET /bus/ws` — the socket a second client watches a session over."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer
from langchain_core.messages import AIMessage

from daimon_agent.events import done_event, step_event, user_event
from daimon_agent.server import create_app
from test_server import fake_graph_builder


@pytest.fixture
async def client(settings):
    app = await create_app(settings, graph_builder=fake_graph_builder)
    async with TestClient(TestServer(app)) as client:
        yield client


async def _call(ws, op: str, **fields) -> dict:
    """Send one op and read frames until its ack, returning (ack, frames)."""
    await ws.send_json({"op": op, "op_id": op, **fields})
    frames = []
    while True:
        frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
        if frame["kind"] == "ack" and frame.get("op_id") == op:
            return frame, frames
        frames.append(frame)


async def test_attaching_replays_what_already_happened(client: TestClient) -> None:
    emit = client.app["bus"].publisher("s")
    emit(user_event("what happened?"))
    emit(step_event("1", "Thinking", "done"))

    async with client.ws_connect("/bus/ws") as ws:
        ack, frames = await _call(ws, "attach", session="s")
        assert ack["ok"] is True
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert [e["type"] for e in snapshot["events"]] == ["user", "step"]
        assert snapshot["seq"] == 2


async def test_a_cursor_inside_the_ring_replays_exactly_the_tail(client: TestClient) -> None:
    emit = client.app["bus"].publisher("s")
    for i in range(5):
        emit(step_event(str(i), "t", "done"))

    async with client.ws_connect("/bus/ws") as ws:
        _ack, frames = await _call(ws, "attach", session="s", since=3)
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert [e["id"] for e in snapshot["events"]] == ["3", "4"]
        assert snapshot["truncated"] is False


async def test_attaching_with_no_cursor_is_a_clean_start(client: TestClient) -> None:
    """`truncated` tells the client to reset rather than splice — a fresh
    snapshot does not join up with whatever it had before."""
    client.app["bus"].publisher("s")(done_event("x"))
    async with client.ws_connect("/bus/ws") as ws:
        _ack, frames = await _call(ws, "attach", session="s")
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert snapshot["truncated"] is True


async def test_a_watcher_sees_a_turn_it_did_not_start(client: TestClient) -> None:
    """The whole point: the phone is attached, the laptop starts a turn."""
    client.app["graph"].router._pro.script = [AIMessage(content="42")]

    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="live")

        resp = await client.post(
            "/task", json={"instruction": "what is it?", "session_id": "live"}
        )
        await resp.read()

        seen, seqs = [], []
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
            if frame["kind"] != "event":
                continue
            seen.append(frame["event"])
            seqs.append(frame["seq"])
            if frame["event"]["type"] == "done":
                break

        assert [e["type"] for e in seen][:2] == ["user", "step"]
        assert seen[-1]["result"] == "42"
        # Contiguous, with no gap — a client can trust its cursor to mean
        # "everything up to here", which is what reconnecting depends on.
        assert seqs == list(range(1, len(seqs) + 1))


async def test_events_carry_a_monotonic_sequence(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="s")
        emit = client.app["bus"].publisher("s")
        for i in range(3):
            emit(step_event(str(i), "t", "done"))

        seqs = []
        for _ in range(3):
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
            seqs.append(frame["seq"])
        assert seqs == [1, 2, 3]


async def test_one_socket_can_watch_several_sessions(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="a")
        await _call(ws, "attach", session="b")
        client.app["bus"].publisher("a")(done_event("from a"))
        client.app["bus"].publisher("b")(done_event("from b"))

        got = {}
        for _ in range(2):
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
            got[frame["session"]] = frame["event"]["result"]
        assert got == {"a": "from a", "b": "from b"}


async def test_detaching_stops_the_stream(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="s")
        ack, _ = await _call(ws, "detach", session="s")
        assert ack["ok"] is True
        assert client.app["bus"].channel("s").subscriber_count == 0


async def test_attaching_twice_to_one_session_is_refused(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="s")
        ack, _ = await _call(ws, "attach", session="s")
        assert ack["ok"] is False
        assert "already attached" in ack["error"]


async def test_a_bad_op_is_answered_not_fatal(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "nonsense")
        assert ack["ok"] is False
        # The socket still works.
        ack, _ = await _call(ws, "ping")
        assert ack["ok"] is True


async def test_attach_without_a_session_is_refused(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "attach")
        assert ack["ok"] is False


async def test_closing_the_socket_releases_every_subscription(client: TestClient) -> None:
    """Nothing tells the server when a phone goes into a tunnel; the socket
    closing has to be enough, or subscribers leak and the server never idles
    out."""
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="a")
        await _call(ws, "attach", session="b")

    for _ in range(50):
        await asyncio.sleep(0.02)
        if all(c.subscriber_count == 0 for c in client.app["bus"].channels()):
            break
    assert [c.subscriber_count for c in client.app["bus"].channels()] == [0, 0]


async def test_the_session_directory_lists_live_and_recorded_sessions(client: TestClient) -> None:
    client.app["graph"].router._pro.script = [AIMessage(content="done")]
    await (await client.post(
        "/task", json={"instruction": "recorded work", "session_id": "recorded", "origin": "cli"}
    )).read()
    client.app["bus"].publisher("live-only")(step_event("1", "t", "running"))

    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "sessions")

    by_id = {s["session_id"]: s for s in ack["sessions"]}
    assert by_id["recorded"]["title"] == "recorded work"
    assert by_id["recorded"]["origin"] == "cli"
    assert "live-only" in by_id  # never written to the log, still listed
    assert all(s["busy"] is False for s in by_id.values())


async def test_a_session_parked_on_a_question_says_so(client: TestClient) -> None:
    """The badge that makes remote worth opening: which sessions want you."""
    from daimon_agent.events import ask_event

    client.app["bus"].publisher("parked")(ask_event("a1", "question", "which?", []))

    async with client.ws_connect("/bus/ws") as ws:
        _ack, frames = await _call(ws, "attach", session="parked")
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert snapshot["pending_ask"]["id"] == "a1"

        ack, _ = await _call(ws, "sessions")
        assert [s for s in ack["sessions"] if s["session_id"] == "parked"][0]["pending_ask"] is True


async def test_todos_come_with_the_snapshot(client: TestClient) -> None:
    from daimon_agent.events import todo_event

    items = [{"text": "step one", "status": "in_progress"}]
    client.app["bus"].publisher("s")(todo_event(items))

    async with client.ws_connect("/bus/ws") as ws:
        _ack, frames = await _call(ws, "attach", session="s")
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]
        assert snapshot["todos"] == items


# --- terminals ---------------------------------------------------------------

async def _read_until(ws, predicate, timeout: float = 15.0):
    """Collect frames until one satisfies `predicate`, returning them all."""
    frames = []

    async def poll():
        while True:
            frame = await ws.receive_json()
            frames.append(frame)
            if predicate(frame):
                return

    await asyncio.wait_for(poll(), timeout=timeout)
    return frames


def _term_text(frames) -> bytes:
    import base64

    return b"".join(
        base64.b64decode(f["data"]) for f in frames if f.get("kind") == "term"
    )


async def _shell_ready(ws, term_id: str) -> None:
    """A login shell starts its line editor asynchronously; bytes sent before
    then are swallowed. Probe until one comes back."""
    while True:
        await ws.send_json({"op": "term.input", "op_id": "probe",
                            "id": term_id, "data": "printf 'WS-%s\\n' READY\n"})
        try:
            frames = await _read_until(
                ws, lambda f: b"WS-READY" in _term_text([f]), timeout=1.5
            )
            return
        except asyncio.TimeoutError:
            continue


async def test_a_terminal_can_be_opened_and_typed_into(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "term.open", cols=100, rows=40)
        assert ack["ok"] is True
        term_id = ack["terminal"]["id"]
        assert ack["terminal"]["pid"] > 0

        await _call(ws, "term.attach", id=term_id, cols=100, rows=40)
        await _shell_ready(ws, term_id)

        await ws.send_json({"op": "term.input", "op_id": "x", "id": term_id,
                            "data": "printf 'FROM-%s\\n' SOCKET\n"})
        frames = await _read_until(ws, lambda f: b"FROM-SOCKET" in _term_text([f]))
        assert b"FROM-SOCKET" in _term_text(frames)


async def test_attaching_to_a_terminal_replays_its_scrollback(client: TestClient) -> None:
    """The reason terminals moved here: a client arriving later used to get a
    blank screen, because the only buffer was the webview's."""
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "term.open")
        term_id = ack["terminal"]["id"]
        await _call(ws, "term.attach", id=term_id)
        await _shell_ready(ws, term_id)
        await ws.send_json({"op": "term.input", "op_id": "x", "id": term_id,
                            "data": "printf 'SCROLL-%s\\n' BACK\n"})
        await _read_until(ws, lambda f: b"SCROLL-BACK" in _term_text([f]))

    # A brand-new socket, as a phone opening the app would be.
    async with client.ws_connect("/bus/ws") as ws2:
        _ack, frames = await _call(ws2, "term.attach", id=term_id)
        (snapshot,) = [f for f in frames if f.get("control") == "term_snapshot"]
        import base64

        assert b"SCROLL-BACK" in base64.b64decode(snapshot["data"])


async def test_two_sockets_share_one_shell(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as a, client.ws_connect("/bus/ws") as b:
        ack, _ = await _call(a, "term.open")
        term_id = ack["terminal"]["id"]
        await _call(a, "term.attach", id=term_id)
        await _call(b, "term.attach", id=term_id)
        await _shell_ready(a, term_id)

        # Typed on one socket, seen on the other.
        await a.send_json({"op": "term.input", "op_id": "x", "id": term_id,
                           "data": "printf 'SHARED-%s\\n' SCREEN\n"})
        frames = await _read_until(b, lambda f: b"SHARED-SCREEN" in _term_text([f]))
        assert b"SHARED-SCREEN" in _term_text(frames)


async def test_the_pty_takes_the_smallest_attached_size(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as desktop, client.ws_connect("/bus/ws") as phone:
        ack, _ = await _call(desktop, "term.open", cols=200, rows=50)
        term_id = ack["terminal"]["id"]
        await _call(desktop, "term.attach", id=term_id, cols=200, rows=50)

        ack, _ = await _call(phone, "term.attach", id=term_id, cols=60, rows=30)
        assert (ack["terminal"]["cols"], ack["terminal"]["rows"]) == (60, 30)


async def test_a_terminal_survives_the_socket_that_opened_it(client: TestClient) -> None:
    """The other half of moving terminals off the app: closing the window is
    not the same as ending the shell."""
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "term.open")
        term_id = ack["terminal"]["id"]
        await _call(ws, "term.attach", id=term_id)

    assert client.app["terminals"].get(term_id) is not None
    assert client.app["terminals"].live_count == 1

    async with client.ws_connect("/bus/ws") as ws2:
        ack, _ = await _call(ws2, "term.list")
        assert [t["id"] for t in ack["terminals"]] == [term_id]


async def test_closing_a_socket_detaches_but_does_not_close_terminals(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "term.open")
        term_id = ack["terminal"]["id"]
        await _call(ws, "term.attach", id=term_id)
        assert client.app["terminals"].get(term_id).describe()["clients"] == 1

    for _ in range(50):
        await asyncio.sleep(0.02)
        if client.app["terminals"].get(term_id).describe()["clients"] == 0:
            break
    assert client.app["terminals"].get(term_id).describe()["clients"] == 0


async def test_closing_a_terminal_ends_it(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "term.open")
        term_id = ack["terminal"]["id"]
        ack, _ = await _call(ws, "term.close", id=term_id)
        assert ack["closed"] is True
        assert client.app["terminals"].get(term_id) is None

        ack, _ = await _call(ws, "term.close", id=term_id)
        assert ack["closed"] is False  # closing twice is not an error


async def test_the_shell_exiting_is_announced(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "term.open")
        term_id = ack["terminal"]["id"]
        await _call(ws, "term.attach", id=term_id)
        await _shell_ready(ws, term_id)

        await ws.send_json({"op": "term.input", "op_id": "x", "id": term_id, "data": "exit 7\n"})
        frames = await _read_until(ws, lambda f: f.get("control") == "term_exited")
        (exited,) = [f for f in frames if f.get("control") == "term_exited"]
        assert (exited["id"], exited["code"]) == (term_id, 7)


async def test_input_to_an_unknown_terminal_is_refused_not_fatal(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "term.input", id="nope", data="hi")
        assert ack["ok"] is False
        ack, _ = await _call(ws, "ping")
        assert ack["ok"] is True


async def test_resizing_a_terminal_that_is_not_there_yet_is_not_an_error(client: TestClient) -> None:
    """A ResizeObserver can fire before the spawn completes; that ordinary
    startup race must not surface as a visible failure."""
    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "term.resize", id="not-yet", cols=80, rows=24)
        assert ack["ok"] is True


async def test_opening_a_terminal_with_a_chosen_id_is_idempotent(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        first, _ = await _call(ws, "term.open", id="tab-1")
        second, _ = await _call(ws, "term.open", id="tab-1")
        assert first["terminal"]["pid"] == second["terminal"]["pid"]
        assert len(client.app["terminals"].list()) == 1


async def test_binary_input_can_be_sent_base64(client: TestClient) -> None:
    """Control sequences are bytes, not text — Ctrl-C is 0x03."""
    import base64

    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "term.open")
        term_id = ack["terminal"]["id"]
        await _call(ws, "term.attach", id=term_id)
        await _shell_ready(ws, term_id)

        await ws.send_json({"op": "term.input", "op_id": "s", "id": term_id,
                            "data": "sleep 30\n"})
        await asyncio.sleep(0.6)
        await ws.send_json({"op": "term.input", "op_id": "c", "id": term_id,
                            "data": base64.b64encode(b"\x03").decode(), "encoding": "base64"})
        # The proof the interrupt landed: the shell takes a new command. (Not
        # "the rest of the line runs" — Ctrl-C abandons the whole line.)
        await ws.send_json({"op": "term.input", "op_id": "a", "id": term_id,
                            "data": "printf 'AFTER-%s\\n' INT\n"})
        frames = await _read_until(ws, lambda f: b"AFTER-INT" in _term_text([f]))
        assert b"AFTER-INT" in _term_text(frames)


# --- prompting ---------------------------------------------------------------

async def test_a_client_can_start_a_turn_and_watch_it(client: TestClient) -> None:
    """The difference from POST /task: no second connection holds the body.
    The result arrives as ordinary events on the socket already open."""
    client.app["graph"].router._pro.script = [AIMessage(content="from the socket")]

    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="remote-1")
        ack, _ = await _call(ws, "prompt", session="remote-1", instruction="do the thing")
        assert ack["ok"] is True

        seen = []
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if frame["kind"] != "event":
                continue
            seen.append(frame["event"])
            if frame["event"]["type"] in ("done", "error"):
                break

        assert seen[0]["type"] == "user"
        assert seen[0]["text"] == "do the thing"
        assert seen[-1]["result"] == "from the socket"


async def test_a_prompt_records_where_it_came_from(client: TestClient) -> None:
    client.app["graph"].router._pro.script = [AIMessage(content="ok")]
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="s")
        await _call(ws, "prompt", session="s", instruction="hello")
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if frame.get("kind") == "event" and frame["event"]["type"] in ("done", "error"):
                break

    client.app["eventlog"].flush()
    assert client.app["eventlog"].replay("s")[0]["origin"] == "remote"


async def test_a_prompt_needs_a_session_and_an_instruction(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        blank, _ = await _call(ws, "prompt", session="s", instruction="   ")
        missing, _ = await _call(ws, "prompt", instruction="hi")
        assert blank["ok"] is False and missing["ok"] is False


async def test_asking_is_only_offered_to_a_client_that_said_it_can_answer(client: TestClient) -> None:
    """An agent that asks a question nobody will answer has hung, not paused."""
    from daimon_agent.graph import ASK_TOOLS

    client.app["graph"].router._pro.script = [AIMessage(content="ok")]
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="watcher")  # wants_ask defaults off
        await _call(ws, "prompt", session="watcher", instruction="go")
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if frame.get("kind") == "event" and frame["event"]["type"] in ("done", "error"):
                break

    state = await client.app["graph"].aget_state(
        {"configurable": {"thread_id": "watcher"}}
    )
    assert "ask" not in (state.values.get("capabilities") or [])


async def test_a_client_that_can_answer_gets_the_ask_capability(client: TestClient) -> None:
    client.app["graph"].router._pro.script = [AIMessage(content="ok")]
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "attach", session="answerer", wants_ask=True)
        await _call(ws, "prompt", session="answerer", instruction="go")
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if frame.get("kind") == "event" and frame["event"]["type"] in ("done", "error"):
                break

    state = await client.app["graph"].aget_state(
        {"configurable": {"thread_id": "answerer"}}
    )
    assert "ask" in (state.values.get("capabilities") or [])


async def test_a_turn_started_over_the_socket_outlives_the_socket(client: TestClient) -> None:
    """A phone going into a tunnel mid-turn must not cancel work the laptop is
    also watching — which is exactly why the turn is detached rather than tied
    to the connection that asked for it."""
    started = asyncio.Event()
    release = asyncio.Event()

    original = client.app["graph"].router._pro._astream

    async def blocking(*args, **kwargs):
        started.set()
        await release.wait()
        async for chunk in original(*args, **kwargs):
            yield chunk

    client.app["graph"].router._pro._astream = blocking
    client.app["graph"].router._pro.script = [AIMessage(content="finished anyway")]

    watcher = client.app["bus"].subscribe("detached")
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "prompt", session="detached", instruction="slow")
        await asyncio.wait_for(started.wait(), timeout=10)

    release.set()  # the socket is closed; the turn is not

    events = []
    async def drain():
        while True:
            item = await watcher.queue.get()
            if item is None:
                return
            events.append(item[1])
            if item[1]["type"] in ("done", "error"):
                return

    await asyncio.wait_for(drain(), timeout=15)
    assert events[-1]["result"] == "finished anyway"


# --- watching every session --------------------------------------------------

async def test_watching_reports_asks_on_sessions_never_attached_to(client: TestClient) -> None:
    """Attach cannot answer this: to attach you must already know the session
    id, and the question is whether *anything* started waiting on you."""
    from daimon_agent.events import ask_event

    async with client.ws_connect("/bus/ws") as ws:
        ack, _ = await _call(ws, "watch")
        assert ack["ok"] is True

        client.app["bus"].publisher("never-seen")(ask_event("a1", "question", "which?", []))

        frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert frame["control"] == "watch"
        assert frame["session"] == "never-seen"
        assert frame["event"]["id"] == "a1"


async def test_watching_ignores_the_noise(client: TestClient) -> None:
    """Knowing something happened is the point; mirroring a transcript nobody
    is reading is not."""
    from daimon_agent.events import assistant_delta_event, done_event, step_event

    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "watch")
        emit = client.app["bus"].publisher("noisy")
        emit(step_event("1", "t", "running"))
        emit(assistant_delta_event("chatter"))
        emit(done_event("finished"))

        frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
        assert frame["event"]["type"] == "done"  # the step and delta were skipped


async def test_a_watcher_does_not_keep_the_server_alive(client: TestClient) -> None:
    """A gateway holding this open so it can notify you must not stop every
    workspace on the machine from ever idling out."""
    from daimon_agent.events import ask_event

    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "watch")
        client.app["bus"].publisher("s")(ask_event("a1", "question", "q", []))
        await asyncio.wait_for(ws.receive_json(), timeout=5)

        # Watching is an observer on the bus, not a subscriber on a channel —
        # which is precisely why it cannot count as somebody looking.
        assert client.app["bus"]._observers
        assert sum(c.subscriber_count for c in client.app["bus"].channels()) == 0


async def test_closing_the_socket_stops_watching(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "watch")
    for _ in range(50):
        await asyncio.sleep(0.02)
        if not client.app["bus"]._observers:
            break
    assert not client.app["bus"]._observers


async def test_an_unserializable_reply_is_answered_not_silence(client: TestClient) -> None:
    """An op returning something json cannot represent used to kill the
    writer: the socket went quiet with no ack and no close, and the client
    waited forever for an answer that was never coming."""
    from pathlib import Path as _Path

    async with client.ws_connect("/bus/ws") as ws:
        # Reach in and make one op misbehave the way a dataclass or a Path
        # returned by accident would.
        await ws.send_json({"op": "attach", "op_id": "warm", "session": "s"})
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
            if frame.get("kind") == "ack":
                break

        client.app["bus"].publisher("s")({"type": "step", "id": _Path("/not/json")})
        ack, _ = await _call(ws, "ping")
        assert ack["ok"] is True  # the socket still works


async def test_two_clients_on_one_session_each_see_what_the_other_sends(client: TestClient) -> None:
    """A phone and a desktop on the same conversation. Whoever types, both
    see it — which is only true because a turn belongs to the session rather
    than to the connection that started it."""
    client.app["graph"].router._pro.script = [
        AIMessage(content="answering the desktop"),
        AIMessage(content="answering the phone"),
    ]

    async def next_turn(ws) -> list[dict]:
        seen = []
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=20)
            if frame.get("kind") != "event":
                continue
            seen.append(frame["event"])
            if frame["event"]["type"] in ("done", "error"):
                return seen

    async with client.ws_connect("/bus/ws") as desktop, client.ws_connect("/bus/ws") as phone:
        await _call(desktop, "attach", session="shared")
        await _call(phone, "attach", session="shared")

        # Typed on the desktop.
        await desktop.send_json({"op": "prompt", "session": "shared",
                                 "instruction": "from the desktop"})
        on_desktop, on_phone = await asyncio.gather(next_turn(desktop), next_turn(phone))
        assert on_desktop[0]["text"] == "from the desktop"
        assert on_phone[0]["text"] == "from the desktop"  # the phone saw it too
        assert on_desktop[-1]["result"] == on_phone[-1]["result"] == "answering the desktop"

        # And the other way round.
        await phone.send_json({"op": "prompt", "session": "shared",
                               "instruction": "from the phone"})
        on_desktop, on_phone = await asyncio.gather(next_turn(desktop), next_turn(phone))
        assert on_desktop[0]["text"] == "from the phone"  # the desktop saw it
        assert on_desktop[-1]["result"] == on_phone[-1]["result"] == "answering the phone"


async def test_attaching_replays_a_conversation_this_client_never_saw(client: TestClient) -> None:
    """Reload the desktop app and the conversation is still there — it used to
    live only in React state, so a refresh lost it."""
    client.app["graph"].router._pro.script = [AIMessage(content="remembered")]
    await (await client.post("/task", json={
        "instruction": "something earlier", "session_id": "history",
    })).read()

    async with client.ws_connect("/bus/ws") as ws:
        _ack, frames = await _call(ws, "attach", session="history")
        (snapshot,) = [f for f in frames if f.get("control") == "snapshot"]

    types = [e["type"] for e in snapshot["events"]]
    assert types[0] == "user"
    assert snapshot["events"][0]["text"] == "something earlier"
    assert snapshot["events"][-1]["result"] == "remembered"


async def test_a_terminal_opened_elsewhere_is_announced(client: TestClient) -> None:
    """A client cannot subscribe to a terminal it has never heard of, so the
    set changing has to be pushed rather than discovered."""
    async with client.ws_connect("/bus/ws") as watcher, client.ws_connect("/bus/ws") as other:
        ack, _ = await _call(other, "term.open")
        term_id = ack["terminal"]["id"]

        frame = await asyncio.wait_for(watcher.receive_json(), timeout=5)
        assert frame["control"] == "terminals_changed"
        assert (frame["change"], frame["id"]) == ("opened", term_id)

        await _call(other, "term.close", id=term_id)
        frame = await asyncio.wait_for(watcher.receive_json(), timeout=5)
        assert (frame["change"], frame["id"]) == ("closed", term_id)


async def test_a_closed_socket_stops_hearing_about_terminals(client: TestClient) -> None:
    async with client.ws_connect("/bus/ws") as ws:
        await _call(ws, "ping")
    for _ in range(50):
        await asyncio.sleep(0.02)
        if not client.app["terminals"]._watchers:
            break
    assert not client.app["terminals"]._watchers
