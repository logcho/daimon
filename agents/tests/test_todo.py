"""The visible task list: parsing what the model actually writes, and the
snapshot-not-patch broadcast."""

from __future__ import annotations

import pytest

from daimon_agent.emitter import set_active_emit
from daimon_agent.tools.todo import clear_todos, get_todos, parse_todos, set_todos


@pytest.fixture(autouse=True)
def _clean():
    clear_todos("s")
    yield
    clear_todos("s")


def test_parse_status_pipe_form() -> None:
    items = parse_todos("done|Read the file\nin_progress|Write the fix\npending|Run tests")
    assert [i["status"] for i in items] == ["done", "in_progress", "pending"]
    assert items[1]["text"] == "Write the fix"
    assert [i["id"] for i in items] == ["1", "2", "3"]


def test_parse_accepts_markdown_checkboxes() -> None:
    """A model asked for a task list writes markdown about as often as it
    follows the format."""
    items = parse_todos("- [x] shipped\n- [ ] not yet")
    assert [(i["status"], i["text"]) for i in items] == [
        ("done", "shipped"),
        ("pending", "not yet"),
    ]


def test_parse_accepts_status_synonyms() -> None:
    items = parse_todos("wip|halfway\ncompleted|finished\ntodo|later")
    assert [i["status"] for i in items] == ["in_progress", "done", "pending"]


def test_unknown_status_falls_back_to_pending_without_losing_the_text() -> None:
    """A slightly wrong checklist beats an error — and the text must survive,
    since a dropped item is worse than a mislabelled one."""
    items = parse_todos("banana|Do the thing")
    assert items == [{"id": "1", "text": "banana|Do the thing", "status": "pending"}]


def test_blank_lines_are_skipped() -> None:
    assert parse_todos("\n\n  \ndone|only one\n") == [
        {"id": "1", "text": "only one", "status": "done"}
    ]


def test_set_todos_emits_the_whole_list_every_time() -> None:
    events: list[dict] = []
    set_active_emit(events.append)
    try:
        set_todos("s", "in_progress|first")
        set_todos("s", "done|first\nin_progress|second")
    finally:
        set_active_emit(None)

    assert [e["type"] for e in events] == ["todo", "todo"]
    # A snapshot, not a patch: the second event carries both items.
    assert len(events[0]["items"]) == 1
    assert len(events[1]["items"]) == 2
    assert get_todos("s") == events[1]["items"]


def test_set_todos_reports_progress_back_to_the_model() -> None:
    """The model sees its own list rendered back, so a bad parse is visible to
    it and not only to the user."""
    out = set_todos("s", "done|a\nin_progress|b\npending|c")
    assert "1/3 done" in out
    assert "[x] a" in out and "[~] b" in out and "[ ] c" in out


def test_more_than_one_in_progress_is_called_out() -> None:
    out = set_todos("s", "in_progress|a\nin_progress|b")
    assert "more than one item is in_progress" in out


def test_empty_list_clears() -> None:
    set_todos("s", "pending|a")
    assert set_todos("s", "") == "Todo list cleared."
    assert get_todos("s") == []


def test_lists_are_per_session() -> None:
    set_todos("s", "pending|mine")
    set_todos("other", "pending|theirs")
    try:
        assert get_todos("s")[0]["text"] == "mine"
        assert get_todos("other")[0]["text"] == "theirs"
    finally:
        clear_todos("other")
