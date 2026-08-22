"""The durable record: what a client attaching an hour late gets to see."""

from __future__ import annotations

from pathlib import Path

import pytest

from daimon_agent.eventlog import COALESCE_LIMIT, EventLog
from daimon_agent.events import (
    assistant_delta_event,
    done_event,
    error_event,
    step_event,
    user_event,
)


@pytest.fixture
def log(tmp_path: Path) -> EventLog:
    store = EventLog(tmp_path / "memory" / "events.db")
    yield store
    store.close()


def _record(log: EventLog, session: str, events: list[dict], *, start: int = 1) -> None:
    for i, event in enumerate(events, start=start):
        log.record(session, i, event)


def test_a_recorded_turn_replays_in_order(log: EventLog) -> None:
    _record(log, "s", [
        user_event("what is 2+2?"),
        step_event("1", "Thinking", "running"),
        done_event("4"),
    ])
    log.flush()

    replayed = log.replay("s")
    assert [e["type"] for e in replayed] == ["user", "step", "done"]
    assert replayed[0]["text"] == "what is 2+2?"
    assert replayed[-1]["result"] == "4"


def test_record_does_not_write_until_flushed(log: EventLog) -> None:
    """`record` is called from turn context and must never touch the disk."""
    log.record("s", 1, done_event("x"))
    assert log.replay("s") == []
    log.flush()
    assert len(log.replay("s")) == 1


def test_consecutive_deltas_are_merged_but_the_text_is_preserved(log: EventLog) -> None:
    _record(log, "s", [assistant_delta_event(ch) for ch in "hello world"])
    log.flush()

    replayed = log.replay("s")
    assert len(replayed) == 1  # eleven events, one row
    assert replayed[0]["text"] == "hello world"


def test_deltas_on_different_channels_are_not_merged(log: EventLog) -> None:
    """Reasoning is not the answer — merging them would put the model's
    thinking into its reply."""
    _record(log, "s", [
        assistant_delta_event("the answer "),
        assistant_delta_event("hmm", channel="reasoning"),
        assistant_delta_event("is 42"),
    ])
    log.flush()

    replayed = log.replay("s")
    assert [e["text"] for e in replayed] == ["the answer ", "hmm", "is 42"]


def test_deltas_from_different_agents_are_not_merged(log: EventLog) -> None:
    _record(log, "s", [
        assistant_delta_event("main"),
        assistant_delta_event("sub", agent_id="a1"),
    ])
    log.flush()
    assert [e.get("agent_id") for e in log.replay("s")] == [None, "a1"]


def test_merging_stops_growing_a_row_without_bound(log: EventLog) -> None:
    _record(log, "s", [assistant_delta_event("x" * 64) for _ in range(200)])
    log.flush()
    rows = log.replay("s")
    assert len(rows) > 1
    assert all(len(r["text"]) <= COALESCE_LIMIT + 64 for r in rows)
    assert "".join(r["text"] for r in rows) == "x" * 64 * 200


def test_deltas_are_not_merged_across_sessions(log: EventLog) -> None:
    log.record("a", 1, assistant_delta_event("from a"))
    log.record("b", 2, assistant_delta_event("from b"))
    log.flush()
    assert [e["text"] for e in log.replay("a")] == ["from a"]
    assert [e["text"] for e in log.replay("b")] == ["from b"]


def test_a_finished_turn_prunes_back_to_the_retention_limit(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db", retention=10)
    _record(log, "s", [step_event(str(i), "t", "done") for i in range(50)])
    log.record("s", 51, done_event("finished"))  # pruning happens on turn end
    log.flush()

    assert len(log.replay("s")) <= 10
    # The most recent events are the ones kept.
    assert log.replay("s")[-1]["type"] == "done"
    log.close()


def test_the_session_directory_is_titled_by_the_first_prompt(log: EventLog) -> None:
    _record(log, "s", [user_event("refactor the parser", origin="cli"), done_event("done")])
    log.flush()

    (row,) = log.sessions()
    assert row["title"] == "refactor the parser"
    assert row["origin"] == "cli"
    assert row["turns"] == 1
    assert row["last_result"] == "done"


def test_a_second_prompt_does_not_rename_the_session(log: EventLog) -> None:
    _record(log, "s", [user_event("first thing")])
    log.flush()
    log.record("s", 2, user_event("second thing"))
    log.flush()
    assert log.sessions()[0]["title"] == "first thing"


def test_an_explicit_title_survives_later_prompts(log: EventLog) -> None:
    log.set_title("s", "Parser work")
    log.record("s", 1, user_event("refactor the parser"))
    log.flush()
    assert log.sessions()[0]["title"] == "Parser work"


def test_sessions_are_listed_most_recently_active_first(log: EventLog) -> None:
    log.record("old", 1, user_event("older"))
    log.flush()
    log.record("new", 1, user_event("newer"))
    log.flush()
    assert [r["session_id"] for r in log.sessions()] == ["new", "old"]


def test_an_errored_turn_is_recorded_but_does_not_count_as_a_turn(log: EventLog) -> None:
    _record(log, "s", [user_event("go"), error_event("it broke")])
    log.flush()
    assert log.sessions()[0]["turns"] == 0
    assert [e["type"] for e in log.replay("s")] == ["user", "error"]


def test_deleting_a_session_forgets_it_everywhere(log: EventLog) -> None:
    """Otherwise /clear wipes the conversation and the session list brings it
    straight back."""
    _record(log, "s", [user_event("secret"), done_event("ok")])
    log.flush()
    log.delete_session("s")

    assert log.replay("s") == []
    assert log.sessions() == []


def test_deleting_a_session_drops_what_was_still_queued(log: EventLog) -> None:
    log.record("s", 1, user_event("secret"))  # never flushed
    log.delete_session("s")
    log.flush()
    assert log.replay("s") == []


def test_deleting_one_session_leaves_the_others_alone(log: EventLog) -> None:
    log.record("keep", 1, user_event("keep me"))
    log.record("drop", 1, user_event("drop me"))
    log.flush()
    log.delete_session("drop")
    assert [r["session_id"] for r in log.sessions()] == ["keep"]
    assert len(log.replay("keep")) == 1


def test_flushing_nothing_is_harmless(log: EventLog) -> None:
    assert log.flush() == 0


def test_replay_is_capped_and_returns_the_most_recent(log: EventLog) -> None:
    _record(log, "s", [step_event(str(i), "t", "done") for i in range(100)])
    log.flush()
    replayed = log.replay("s", limit=10)
    assert len(replayed) == 10
    assert [e["id"] for e in replayed] == [str(i) for i in range(90, 100)]
