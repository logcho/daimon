"""The event contract, frozen byte-for-byte against legacy/agents/src/events.ts.
The future Rust daemon proxies this stream to the UI — shapes here must never
drift. Note: legacy's JSON.stringify drops keys whose value is undefined, so
`tool` is absent from tool-less step events, not null."""

from __future__ import annotations

import json

from daimon_agent.events import (
    done_event,
    error_event,
    host_action_close_application,
    host_action_music_control,
    host_action_open_application,
    host_action_open_file,
    live_frame_event,
    step_event,
    ui_action_event,
    user_event,
)


def test_step_event_without_tool_omits_key() -> None:
    assert step_event("id-1", "Thinking", "running") == {
        "type": "step",
        "id": "id-1",
        "label": "Thinking",
        "status": "running",
    }


def test_step_event_with_tool_includes_key() -> None:
    assert step_event("id-2", "read_page", "done", "read_page") == {
        "type": "step",
        "id": "id-2",
        "label": "read_page",
        "status": "done",
        "tool": "read_page",
    }


def test_step_event_with_parent_context() -> None:
    """Sub-agent steps carry parent_step_id and subagent_query for TUI indentation."""
    assert step_event(
        "id-3", "web_search", "running", "web_search",
        parent_step_id="sub-1", subagent_query="latest react features",
    ) == {
        "type": "step",
        "id": "id-3",
        "label": "web_search",
        "status": "running",
        "tool": "web_search",
        "parent_step_id": "sub-1",
        "subagent_query": "latest react features",
    }


def test_step_event_parent_context_omitted_when_none() -> None:
    """parent_step_id and subagent_query are omitted (not null) when not provided,
    matching legacy JSON.stringify(undefined) behavior."""
    ev = step_event("id-4", "Thinking", "running")
    assert "parent_step_id" not in ev
    assert "subagent_query" not in ev


def test_done_and_error() -> None:
    assert done_event("the result") == {"type": "done", "result": "the result"}
    assert error_event("something broke") == {"type": "error", "message": "something broke"}


def test_ui_action_shape() -> None:
    assert ui_action_event("ls -la") == {
        "type": "ui_action",
        "action": "open_terminal_with_command",
        "command": "ls -la",
    }


def test_host_action_discriminated_union_shapes() -> None:
    assert host_action_open_application("Spotify") == {
        "type": "host_action",
        "action": "open_application",
        "name": "Spotify",
    }
    assert host_action_close_application("Spotify") == {
        "type": "host_action",
        "action": "close_application",
        "name": "Spotify",
    }
    assert host_action_music_control("Spotify", "play") == {
        "type": "host_action",
        "action": "music_control",
        "app": "Spotify",
        "command": "play",
    }
    assert host_action_open_file("/tmp/report.xlsx") == {
        "type": "host_action",
        "action": "open_file",
        "path": "/tmp/report.xlsx",
    }


def test_live_frame_shape() -> None:
    assert live_frame_event("aGVsbG8=") == {"type": "live_frame", "data": "aGVsbG8="}


def test_all_events_are_json_serializable() -> None:
    events = [
        step_event("a", "Thinking", "running"),
        step_event("b", "click", "done", "click"),
        done_event("x"),
        error_event("y"),
        ui_action_event("z"),
        live_frame_event("w"),
    ]
    for event in events:
        decoded = json.loads(json.dumps(event))
        assert decoded == event


# --- additions since the port ------------------------------------------------
# New event *types* rather than changed shapes, and new keys omitted when None
# — the same discipline the original port established, so a consumer written
# against the old contract keeps working.

from daimon_agent.events import (  # noqa: E402
    TERMINAL_TYPES,
    ask_event,
    assistant_delta_event,
    compaction_event,
    todo_event,
    usage_event,
)


def test_ask_joins_done_and_error_as_terminal() -> None:
    assert set(TERMINAL_TYPES) == {"done", "error", "ask"}


def test_step_optional_keys_are_omitted_when_absent() -> None:
    """The existing call sites must serialize byte-for-byte as before."""
    event = step_event("s1", "Thinking", "running")
    assert event == {"type": "step", "id": "s1", "label": "Thinking", "status": "running"}


def test_step_carries_detail_and_agent_attribution() -> None:
    event = step_event(
        "s1", "read_file", "done", "read_file",
        detail="src/graph.py", elapsed_ms=420, agent_id="sub-1", agent_label="explore",
    )
    assert event["detail"] == "src/graph.py"
    assert event["elapsed_ms"] == 420
    assert event["agent_id"] == "sub-1"
    assert event["agent_label"] == "explore"


def test_assistant_delta_omits_the_default_channel() -> None:
    assert assistant_delta_event("hi") == {"type": "assistant_delta", "text": "hi"}
    assert assistant_delta_event("hm", channel="reasoning")["channel"] == "reasoning"


def test_usage_omits_cost_for_an_unpriced_model() -> None:
    """Absent means unknown. A zero would read as free."""
    event = usage_event("mystery", 100, 10)
    assert "cost_usd" not in event
    assert usage_event("deepseek-chat", 100, 10, cost_usd=0.001)["cost_usd"] == 0.001


def test_todo_carries_the_whole_list() -> None:
    items = [{"id": "1", "text": "a", "status": "done"}]
    assert todo_event(items) == {"type": "todo", "items": items}


def test_done_carries_optional_usage_totals() -> None:
    assert done_event("hi") == {"type": "done", "result": "hi"}
    assert done_event("hi", usage={"input_tokens": 5})["usage"] == {"input_tokens": 5}


def test_ask_event_shape() -> None:
    event = ask_event(
        "a1", "question", "Which?", [{"label": "A", "description": "first"}],
        header="Choice",
    )
    assert event["type"] == "ask"
    assert event["kind"] == "question"
    assert event["multi_select"] is False
    assert "plan" not in event  # omitted for a plain question
    assert ask_event("a2", "plan", "Go?", [], plan="1. do it")["plan"] == "1. do it"


def test_compaction_event_shape() -> None:
    assert compaction_event(50000, 8000, 24) == {
        "type": "compaction", "before_tokens": 50000, "after_tokens": 8000, "dropped": 24,
    }


def test_user_event_carries_the_prompt() -> None:
    assert user_event("do the thing") == {"type": "user", "text": "do the thing"}


def test_user_event_omits_mode_and_origin_when_absent() -> None:
    """Same discipline as every other constructor here: a key with no value is
    absent, not null, because legacy's JSON.stringify dropped undefined."""
    assert "mode" not in user_event("x")
    assert "origin" not in user_event("x")
    assert user_event("x", mode="plan", origin="remote") == {
        "type": "user",
        "text": "x",
        "mode": "plan",
        "origin": "remote",
    }


def test_user_event_is_not_terminal() -> None:
    """It opens a turn; it does not close a stream."""
    from daimon_agent.events import TERMINAL_TYPES

    assert user_event("x")["type"] not in TERMINAL_TYPES
