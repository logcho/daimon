"""The fan-out contract: many readers, one turn, and no reader able to stall it."""

from __future__ import annotations

import asyncio

import pytest

from daimon_agent.bus import QUEUE_LIMIT, RING_CAPACITY, EventBus, SessionChannel
from daimon_agent.events import ask_event, done_event, step_event, todo_event


def _drain(sub) -> list[dict]:
    """Everything queued for a subscriber right now, as bare events."""
    out = []
    while not sub.queue.empty():
        item = sub.queue.get_nowait()
        if item is None:
            break
        out.append(item[1])
    return out


def test_publish_is_ordered_and_returns_a_monotonic_seq() -> None:
    chan = SessionChannel("s")
    seqs = [chan.publish(step_event(str(i), "t", "done")) for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    assert [e["id"] for _, e in chan.ring] == ["0", "1", "2", "3", "4"]


def test_two_subscribers_see_identical_streams() -> None:
    bus = EventBus()
    a = bus.subscribe("s")
    b = bus.subscribe("s")
    emit = bus.publisher("s")
    for i in range(3):
        emit(step_event(str(i), "t", "done"))
    emit(done_event("finished"))
    assert _drain(a) == _drain(b)


def test_a_subscriber_attached_later_misses_what_it_was_not_there_for() -> None:
    """The queue is live-only; catching up is `backlog`'s job, not the queue's."""
    bus = EventBus()
    early = bus.subscribe("s")
    emit = bus.publisher("s")
    emit(step_event("1", "t", "done"))
    late = bus.subscribe("s")
    emit(step_event("2", "t", "done"))

    assert [e["id"] for e in _drain(early)] == ["1", "2"]
    assert [e["id"] for e in _drain(late)] == ["2"]


def test_backlog_since_a_cursor_returns_exactly_the_tail() -> None:
    chan = SessionChannel("s")
    for i in range(10):
        chan.publish(step_event(str(i), "t", "done"))

    events, cursor, truncated = chan.backlog(since=7)
    assert [e["id"] for e in events] == ["7", "8", "9"]
    assert cursor == 10
    assert truncated is False


def test_backlog_flags_truncation_when_the_ring_no_longer_reaches_the_cursor() -> None:
    """A hole the client cannot see is worse than a visible reset."""
    chan = SessionChannel("s")
    for i in range(RING_CAPACITY + 50):
        chan.publish(step_event(str(i), "t", "done"))

    _events, _cursor, truncated = chan.backlog(since=1)
    assert truncated is True
    # And a cursor still inside the ring is not a truncation.
    _events, _cursor, truncated = chan.backlog(since=chan.seq - 5)
    assert truncated is False


def test_a_full_subscriber_drops_events_without_blocking_the_publisher() -> None:
    """The property the whole design rests on: a phone on a train must not be
    able to stop the agent."""
    bus = EventBus()
    slow = bus.subscribe("s")
    emit = bus.publisher("s")

    for i in range(QUEUE_LIMIT + 10):
        emit(step_event(str(i), "t", "done"))  # returns, every time

    assert slow.lagged is True
    assert slow.queue.qsize() < QUEUE_LIMIT + 10
    # The turn's own record is untouched by one slow reader.
    assert bus.channel("s").seq == QUEUE_LIMIT + 10


def test_a_slow_subscriber_does_not_cost_a_healthy_one_anything() -> None:
    bus = EventBus()
    slow = bus.subscribe("s")
    fast = bus.subscribe("s")
    emit = bus.publisher("s")
    for i in range(QUEUE_LIMIT + 10):
        emit(step_event(str(i), "t", "done"))
        _drain(fast)  # keeps up
    assert slow.lagged is True
    assert fast.lagged is False


def test_ask_is_folded_out_of_the_stream_and_taken_once() -> None:
    """Two clients racing to answer: one wins, the other finds it already gone."""
    chan = SessionChannel("s")
    chan.publish(ask_event("a1", "question", "which?", []))
    assert chan.pending_ask is not None

    assert chan.clear_pending_ask()["id"] == "a1"
    assert chan.clear_pending_ask() is None


def test_a_finished_turn_is_not_parked_on_a_question() -> None:
    chan = SessionChannel("s")
    chan.publish(ask_event("a1", "question", "which?", []))
    chan.publish(done_event("answered"))
    assert chan.pending_ask is None


def test_todos_are_kept_as_the_latest_snapshot() -> None:
    chan = SessionChannel("s")
    chan.publish(todo_event([{"text": "one", "status": "pending"}]))
    chan.publish(todo_event([{"text": "one", "status": "done"}]))
    assert chan.todos == [{"text": "one", "status": "done"}]


def test_ask_capability_reflects_who_is_actually_attached() -> None:
    """The agent is only offered the ask tools when somebody can answer."""
    bus = EventBus()
    chan = bus.channel("s")
    assert chan.has_ask_capable_subscriber is False

    watcher = bus.subscribe("s")
    assert chan.has_ask_capable_subscriber is False

    answerer = bus.subscribe("s", wants_ask=True)
    assert chan.has_ask_capable_subscriber is True

    bus.unsubscribe("s", answerer)
    assert chan.has_ask_capable_subscriber is False
    bus.unsubscribe("s", watcher)


def test_unsubscribe_wakes_the_reader() -> None:
    bus = EventBus()
    sub = bus.subscribe("s")
    bus.unsubscribe("s", sub)
    assert sub.queue.get_nowait() is None


def test_release_drops_the_channel_and_closes_its_readers() -> None:
    bus = EventBus()
    sub = bus.subscribe("s")
    bus.publisher("s")(step_event("1", "t", "done"))

    bus.release("s")
    assert bus.peek("s") is None
    # What was already queued still drains, then the end-of-stream sentinel —
    # a reader unwinds rather than hanging on a channel that is never coming
    # back.
    assert [e["id"] for e in _drain(sub)] == ["1"]
    assert sub.queue.empty()
    # A later publish starts a brand-new channel rather than resurrecting one.
    assert bus.channel("s").seq == 0


def test_the_sink_sees_every_event_with_its_sequence() -> None:
    seen: list[tuple[str, int, str]] = []
    bus = EventBus(sink=lambda sid, seq, ev: seen.append((sid, seq, ev["type"])))
    emit = bus.publisher("s")
    emit(step_event("1", "t", "done"))
    emit(done_event("x"))
    assert seen == [("s", 1, "step"), ("s", 2, "done")]


async def test_a_reader_awaiting_the_queue_is_woken_by_a_publish() -> None:
    """The subscriber queue is an asyncio queue, so a reader parked on it must
    wake on a synchronous publish from turn context."""
    bus = EventBus()
    sub = bus.subscribe("s")

    async def read_one():
        return await sub.queue.get()

    task = asyncio.create_task(read_one())
    await asyncio.sleep(0)
    bus.publisher("s")(done_event("hello"))
    seq, event = await asyncio.wait_for(task, timeout=1)
    assert (seq, event["result"]) == (1, "hello")


# --- idle shutdown ----------------------------------------------------------

async def test_a_watching_client_keeps_the_server_alive() -> None:
    """A phone reading a session is a reason not to exit, even with no turn
    running — otherwise the server dies under whoever is looking at it."""
    from daimon_agent.server import _idle_shutdown_loop

    bus = EventBus()
    bus.subscribe("s")
    reg = {"count": 0, "sessions": {}, "lock": asyncio.Lock(), "last_active_at": -1e9}
    killed: list[int] = []

    async def fake_sleep(_seconds: float) -> None:
        if len(killed) or fake_sleep.ticks > 3:
            raise asyncio.CancelledError
        fake_sleep.ticks += 1

    fake_sleep.ticks = 0
    with pytest.raises(asyncio.CancelledError):
        await _idle_shutdown_loop(
            reg, 1.0,
            attached=lambda: sum(c.subscriber_count for c in bus.channels()),
            poll_interval=0.0, sleep=fake_sleep, kill=lambda pid, sig: killed.append(pid),
        )
    assert killed == []


async def test_with_nobody_watching_the_server_still_exits() -> None:
    from daimon_agent.server import _idle_shutdown_loop

    bus = EventBus()
    reg = {"count": 0, "sessions": {}, "lock": asyncio.Lock(), "last_active_at": -1e9}
    killed: list[int] = []
    await _idle_shutdown_loop(
        reg, 1.0,
        attached=lambda: sum(c.subscriber_count for c in bus.channels()),
        poll_interval=0.0, sleep=lambda _s: asyncio.sleep(0),
        kill=lambda pid, sig: killed.append(pid),
    )
    assert killed  # it self-terminated
