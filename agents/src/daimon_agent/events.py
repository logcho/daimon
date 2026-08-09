"""Daimon's TaskEvent NDJSON contract — a byte-for-byte port of
`legacy/agents/src/events.ts`. The future Rust daemon proxies this stream to
the UI, so the field names and shapes here are frozen; `tests/test_events.py`
locks them down.

Notes on fidelity: the legacy serializer was `JSON.stringify`, which drops
keys whose value is `undefined` — so `tool` is omitted from a `step` event
when it has no tool (e.g. the Thinking step), rather than serialized as null.
Constructors here replicate that.
"""

from __future__ import annotations

from typing import Literal

StepStatus = Literal["pending", "running", "done", "error"]

# --- step -------------------------------------------------------------------
def step_event(
    id: str,
    label: str,
    status: StepStatus,
    tool: str | None = None,
    *,
    parent_step_id: str | None = None,
    subagent_query: str | None = None,
) -> dict:
    event: dict = {"type": "step", "id": id, "label": label, "status": status}
    if tool is not None:
        event["tool"] = tool
    if parent_step_id is not None:
        event["parent_step_id"] = parent_step_id
    if subagent_query is not None:
        event["subagent_query"] = subagent_query
    return event


# --- done / error ------------------------------------------------------------
def done_event(result: str) -> dict:
    return {"type": "done", "result": result}


def error_event(message: str) -> dict:
    return {"type": "error", "message": message}


# --- ui_action (frontend side-effect; stage-don't-execute) -------------------
def ui_action_event(command: str) -> dict:
    return {"type": "ui_action", "action": "open_terminal_with_command", "command": command}


# --- host_action (daemon side-effect; discriminated union) -------------------
def host_action_open_application(name: str) -> dict:
    return {"type": "host_action", "action": "open_application", "name": name}


def host_action_close_application(name: str) -> dict:
    return {"type": "host_action", "action": "close_application", "name": name}


def host_action_music_control(app: Literal["Spotify", "Music"], command: Literal["play", "pause", "next", "previous"]) -> dict:
    return {"type": "host_action", "action": "music_control", "app": app, "command": command}


def host_action_open_file(path: str) -> dict:
    return {"type": "host_action", "action": "open_file", "path": path}


# --- live_frame --------------------------------------------------------------
def live_frame_event(data: str) -> dict:
    return {"type": "live_frame", "data": data}
