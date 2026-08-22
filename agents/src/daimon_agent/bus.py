"""Per-session event fan-out.

`emitter.py` holds exactly one callable, and `server._stream_turn_response`
used to build that callable per HTTP request — so the socket that started a
turn was the only thing that would ever see it, and anything emitted before a
client connected was gone. That is fine for one CLI on one machine; it is the
whole problem for a phone that wants to watch a turn already in flight.

This module is the seam. `publish()` is called once per event; every attached
`Subscriber` gets its own copy, and a bounded ring keeps the recent past so a
late joiner can be handed the tail of the turn instead of a blank screen.

Two invariants matter more than anything else here:

- **`publish` is synchronous and never awaits.** Graph nodes call `emit()` from
  sync context (including sync tools running in worker threads), and ordering
  across that boundary is only preserved because the call itself cannot yield.
  Every I/O this triggers — the socket write, the event-log insert — is an
  enqueue.
- **A slow subscriber must never stall a turn.** A phone on a train is a slow
  subscriber. When its queue fills we drop the oldest half and flag it
  `lagged`; the reader notices, tells the client to resync, and the turn never
  knows it happened. The alternative — backpressure all the way to the model —
  means a backgrounded browser tab can freeze the agent.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterable

#: Events kept per session for replay. Two thousand covers several turns of a
#: normal conversation; a phone attaching mid-turn wants the tail, not history
#: (that is the event log's job, `eventlog.py`).
RING_CAPACITY = 2000

#: What a subscriber with no cursor gets on attach. Less than the ring, because
#: "catch me up" means the current conversation, not everything we still hold.
DEFAULT_BACKLOG = 800

#: Per-subscriber queue depth before we start dropping. Generous enough that a
#: momentary stall (a re-render, a reconnect) never trips it.
QUEUE_LIMIT = 512


class Subscriber:
    """One reader of one session's stream.

    Holds `(seq, event)` pairs rather than bare events so a reconnecting client
    can say "I have up to 412" and be given exactly what it missed.
    """

    __slots__ = ("queue", "lagged", "wants_ask", "label", "_closed")

    def __init__(self, *, wants_ask: bool = False, label: str = "") -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        #: Set when we dropped events to keep up. The reader is expected to
        #: turn this into a resync rather than silently serving a stream with a
        #: hole in it.
        self.lagged = False
        #: Whether this client can answer an `ask`. Read by the capability
        #: negotiation in server.py — the agent is only offered the ask tools
        #: when somebody attached can actually answer.
        self.wants_ask = wants_ask
        self.label = label
        self._closed = False

    def offer(self, record: tuple[int, dict]) -> None:
        """Hand this subscriber one event. Never blocks, never raises."""
        if self._closed:
            return
        if self.queue.qsize() >= QUEUE_LIMIT:
            # Drop the oldest half rather than the newest: what a lagging
            # client needs is to get near the present quickly, and it is about
            # to be told to resync anyway.
            for _ in range(QUEUE_LIMIT // 2):
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self.lagged = True
        self.queue.put_nowait(record)

    def close(self) -> None:
        """Wake the reader with the end-of-stream sentinel."""
        if self._closed:
            return
        self._closed = True
        self.queue.put_nowait(None)


class SessionChannel:
    """Everything the server knows about one session's live stream."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        #: Monotonic across the process lifetime of this channel. Never resets,
        #: so a cursor stays meaningful for as long as the client holds it.
        self.seq = 0
        self.ring: deque[tuple[int, dict]] = deque(maxlen=RING_CAPACITY)
        self.subs: set[Subscriber] = set()
        #: The ask this session is parked on, if any. Folded out of the stream
        #: so a client attaching to an already-suspended session can render the
        #: prompt immediately instead of waiting for an event that already
        #: happened.
        self.pending_ask: dict | None = None
        #: Last todo snapshot. The `todo` event carries the whole list every
        #: time, so keeping the latest is enough to bring a new client current.
        self.todos: list[dict] = []
        #: The last ask that was answered. Kept so a client retrying a resume
        #: it already won — a flaky phone, a double tap — is told "already
        #: answered" rather than being allowed to resume the turn twice.
        self.answered_ask_id: str | None = None
        self.last_event_at = time.monotonic()

    # --- publishing ---------------------------------------------------------

    def publish(self, event: dict, *, sink: Callable[[str, int, dict], None] | None = None) -> int:
        """Record one event and hand it to every subscriber. Synchronous."""
        self.seq += 1
        record = (self.seq, event)
        self.ring.append(record)
        self.last_event_at = time.monotonic()
        self._fold(event)
        for sub in tuple(self.subs):  # tuple(): offer never mutates, but be safe
            sub.offer(record)
        if sink is not None:
            sink(self.session_id, self.seq, event)
        return self.seq

    def _fold(self, event: dict) -> None:
        """Keep the derived state a late joiner needs up to date."""
        etype = event.get("type")
        if etype == "ask":
            self.pending_ask = event
        elif etype in ("done", "error"):
            # A turn that ended is not parked on a question any more. `ask` is
            # terminal too, which is why it is handled above rather than here.
            self.pending_ask = None
        elif etype == "todo":
            self.todos = event.get("items") or []

    def clear_pending_ask(self) -> dict | None:
        """Take the parked ask, if there is one. Used by /resume to make
        answering a compare-and-swap: two clients race, one wins."""
        ask, self.pending_ask = self.pending_ask, None
        if ask is not None:
            self.answered_ask_id = str(ask.get("id") or "") or None
        return ask

    # --- subscribing --------------------------------------------------------

    def attach(self, sub: Subscriber) -> None:
        self.subs.add(sub)

    def detach(self, sub: Subscriber) -> None:
        self.subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self.subs)

    @property
    def has_ask_capable_subscriber(self) -> bool:
        return any(s.wants_ask for s in self.subs)

    # --- replay -------------------------------------------------------------

    def backlog(self, since: int | None = None) -> tuple[list[dict], int, bool]:
        """Events to replay on attach, as `(events, cursor, truncated)`.

        `truncated` means the ring no longer reaches back to the client's
        cursor, so what it holds does not join up with what we can send. The
        client is expected to reset its transcript rather than splice a hole
        into it — a visibly fresh start beats a silently wrong one.
        """
        if not self.ring:
            return [], self.seq, False
        oldest = self.ring[0][0]
        if since is None:
            tail = list(self.ring)[-DEFAULT_BACKLOG:]
            return [e for _, e in tail], self.seq, oldest > 1
        truncated = since < oldest - 1
        return [e for s, e in self.ring if s > since], self.seq, truncated


class EventBus:
    """The set of live session channels, plus an optional durable sink.

    The sink is `eventlog.EventLog.record` in production. It is injected rather
    than imported so the bus stays testable on its own and so a process that
    does not want a log (a test, a one-shot) simply has none.
    """

    def __init__(self, *, sink: Callable[[str, int, dict], None] | None = None) -> None:
        self._channels: dict[str, SessionChannel] = {}
        self.sink = sink
        #: Called for every event on every session. Subscribing per session
        #: cannot answer "did anything, anywhere, start waiting on me?" —
        #: you would have to know which sessions exist before they do.
        self._observers: set[Callable[[str, int, dict], None]] = set()

    def observe(self, fn: Callable[[str, int, dict], None]) -> Callable[[], None]:
        """Watch every session at once. Returns an unsubscribe."""
        self._observers.add(fn)
        return lambda: self._observers.discard(fn)

    def channel(self, session_id: str) -> SessionChannel:
        chan = self._channels.get(session_id)
        if chan is None:
            chan = SessionChannel(session_id)
            self._channels[session_id] = chan
        return chan

    def peek(self, session_id: str) -> SessionChannel | None:
        """The channel if it exists — without creating one. For read-only
        callers (status, listings) that must not conjure empty sessions."""
        return self._channels.get(session_id)

    def sessions(self) -> Iterable[str]:
        return tuple(self._channels)

    def publisher(self, session_id: str) -> Callable[[dict], None]:
        """The `emit` a turn runs with. Sync, by contract — see the module
        docstring."""
        chan = self.channel(session_id)

        def emit(event: dict) -> None:
            seq = chan.publish(event, sink=self.sink)
            for observer in tuple(self._observers):
                try:
                    observer(session_id, seq, event)
                except Exception:
                    pass  # an observer must never be able to break a turn

        return emit

    def subscribe(
        self, session_id: str, *, wants_ask: bool = False, label: str = ""
    ) -> Subscriber:
        sub = Subscriber(wants_ask=wants_ask, label=label)
        self.channel(session_id).attach(sub)
        return sub

    def unsubscribe(self, session_id: str, sub: Subscriber) -> None:
        chan = self._channels.get(session_id)
        if chan is not None:
            chan.detach(sub)
        sub.close()

    def release(self, session_id: str) -> None:
        """Drop a channel entirely — DELETE /sessions/{id}. Subscribers are
        closed so their readers unwind rather than hanging on a channel that
        will never publish again."""
        chan = self._channels.pop(session_id, None)
        if chan is None:
            return
        for sub in tuple(chan.subs):
            sub.close()
        chan.subs.clear()

    def channels(self) -> Iterable[SessionChannel]:
        return tuple(self._channels.values())
