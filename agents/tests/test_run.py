"""The turn watchdog: what counts as a turn being alive.

The bug this file exists for: the watchdog raced each chunk of
`graph.astream(stream_mode="updates")`, which yields once per *node*. A model
call, a 120s `run_tests`, a fan-out of sub-agents — all one node, all streaming
events the whole time, all longer than the 60s budget. Turns the user could
watch working were killed for producing "no output".
"""

from __future__ import annotations

import asyncio

import pytest

from daimon_agent.run import (
    Activity,
    TurnTimeoutError,
    _stream_with_inactivity_timeout,
)


class SlowStream:
    """One node that takes `duration` and yields a single update at the end."""

    def __init__(self, duration: float, *, updates: int = 1) -> None:
        self.duration = duration
        self.updates = updates
        self.cancelled = False

    async def __aiter__(self):
        for _ in range(self.updates):
            try:
                await asyncio.sleep(self.duration)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            yield {"node": {}}


async def test_a_long_node_that_emits_events_is_not_a_stall() -> None:
    """The whole point. The node takes 3x the budget and never yields, but
    events are flowing, so the turn is alive."""
    activity = Activity()
    stream = SlowStream(0.3)

    async def heartbeat() -> None:
        for _ in range(30):
            await asyncio.sleep(0.02)
            activity.stamp()

    beat = asyncio.create_task(heartbeat())
    await _stream_with_inactivity_timeout(stream, 0.1, activity)
    beat.cancel()

    assert not stream.cancelled


async def test_genuine_silence_still_trips() -> None:
    activity = Activity()
    stream = SlowStream(5.0)

    with pytest.raises(TurnTimeoutError, match="no output"):
        await _stream_with_inactivity_timeout(stream, 0.1, activity)


async def test_a_trip_cancels_the_graph_rather_than_leaking_it() -> None:
    """A stalled turn must actually stop. The consumer owns the graph run, so
    cancelling it is what ends the work."""
    activity = Activity()
    stream = SlowStream(5.0)

    with pytest.raises(TurnTimeoutError):
        await _stream_with_inactivity_timeout(stream, 0.1, activity)

    assert stream.cancelled


async def test_stream_exceptions_still_reach_the_caller() -> None:
    """`_drive` catches GraphRecursionError off this call to continue from the
    checkpoint. Draining in a task must not swallow it."""

    class Exploding:
        async def __aiter__(self):
            yield {"node": {}}
            raise RuntimeError("graph blew up")

    with pytest.raises(RuntimeError, match="graph blew up"):
        await _stream_with_inactivity_timeout(Exploding(), 10.0, Activity())


async def test_a_node_boundary_counts_as_activity_on_its_own() -> None:
    """Nothing needs to emit for a stream that is genuinely producing updates —
    each one stamps the clock itself."""
    stream = SlowStream(0.05, updates=6)  # 0.3s total, well past the budget
    await _stream_with_inactivity_timeout(stream, 0.15, Activity())
    assert not stream.cancelled


def test_tracking_stamps_on_every_event() -> None:
    activity = Activity()
    seen: list[dict] = []
    emit = activity.tracking(seen.append)

    activity._last -= 100  # pretend a long silence
    assert activity.idle_for() > 99

    emit({"type": "step"})

    assert seen == [{"type": "step"}]
    assert activity.idle_for() < 1
