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
