"""Daimon's TaskEvent NDJSON contract — a byte-for-byte port of
`legacy/agents/src/events.ts`. The future Rust daemon proxies this stream to
the UI, so the field names and shapes here are frozen; `tests/test_events.py`
locks them down.

Notes on fidelity: the legacy serializer was `JSON.stringify`, which drops
keys whose value is `undefined` — so `tool` is omitted from a `step` event
when it has no tool (e.g. the Thinking step), rather than serialized as null.
Constructors here replicate that.

Additions since the port (assistant_delta, usage, todo, ask, compaction, and
step's detail/elapsed_ms/agent_* keys) follow the same discipline: new event
*types* rather than changed shapes, and new keys omitted when None. The app's
`applyEvent` has a `default: return messages` arm, so it ignores everything it
doesn't know — a consumer built against the original contract keeps working.

`done`, `error`, and `ask` are the terminal events (mutually exclusive). `ask`
means the turn suspended on a LangGraph interrupt and is waiting for an answer
via POST /resume — it is a pause, not an ending, but it closes the stream.
"""

from __future__ import annotations

from typing import Any, Literal

StepStatus = Literal["pending", "running", "done", "error"]

#: Event types that end a stream. The server closes the response body right
#: after writing one of these; the client stops reading at it.
TERMINAL_TYPES = ("done", "error", "ask")


# --- step -------------------------------------------------------------------
def step_event(
    id: str,
    label: str,
    status: StepStatus,
    tool: str | None = None,
    *,
    parent_step_id: str | None = None,
    subagent_query: str | None = None,
    detail: str | None = None,
    elapsed_ms: int | None = None,
    agent_id: str | None = None,
    agent_label: str | None = None,
) -> dict:
    """One tool/step lifecycle event.

    `detail` is a short human summary of the call's arguments (the path, the
    query) for display next to the tool name. `agent_id`/`agent_label` name the
    sub-agent a step belongs to, so a UI can group several sub-agents running
    concurrently rather than interleaving their steps.
    """
    event: dict = {"type": "step", "id": id, "label": label, "status": status}
    if tool is not None:
        event["tool"] = tool
    if parent_step_id is not None:
        event["parent_step_id"] = parent_step_id
    if subagent_query is not None:
        event["subagent_query"] = subagent_query
    if detail is not None:
        event["detail"] = detail
    if elapsed_ms is not None:
        event["elapsed_ms"] = elapsed_ms
    if agent_id is not None:
        event["agent_id"] = agent_id
    if agent_label is not None:
        event["agent_label"] = agent_label
    return event


# --- assistant text (streamed) ----------------------------------------------
def assistant_delta_event(
    text: str,
    *,
    channel: Literal["text", "reasoning"] = "text",
    agent_id: str | None = None,
) -> dict:
    """A chunk of assistant output as the model produces it.

    `channel` separates the answer from the model's own reasoning trace
    (DeepSeek's `reasoning_content`, Anthropic's thinking blocks) so a UI can
    render the two differently — or drop the reasoning. The full answer text
    still arrives on `done`, so a consumer that ignores deltas loses nothing.
    """
    event: dict = {"type": "assistant_delta", "text": text}
    if channel != "text":
        event["channel"] = channel
    if agent_id is not None:
        event["agent_id"] = agent_id
    return event


# --- usage / cost ------------------------------------------------------------
def usage_event(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float | None = None,
    role: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """Token usage for one model call. `cost_usd` is None for a model with no
    known price — a UI shows tokens only rather than a wrong number."""
    event: dict = {
        "type": "usage",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
    }
    if cost_usd is not None:
        event["cost_usd"] = cost_usd
    if role is not None:
        event["role"] = role
    if agent_id is not None:
        event["agent_id"] = agent_id
    return event


# --- todo list ---------------------------------------------------------------
TodoStatus = Literal["pending", "in_progress", "done"]


def todo_event(items: list[dict]) -> dict:
    """The whole checklist, every time — a snapshot, not a patch, so a consumer
    that missed an earlier event still converges on the right list."""
    return {"type": "todo", "items": items}


# --- continuation ------------------------------------------------------------
def continuation_event(steps: int, max_steps: int, tokens: int = 0) -> dict:
    """The turn hit the graph's step cap and is carrying on from the
    checkpoint. Not an error and not a pause — progress worth showing, so a
    long unattended run reads as working rather than as silence."""
    return {
        "type": "continuation",
        "steps": steps,
        "max_steps": max_steps,
        "tokens": tokens,
    }


# --- compaction --------------------------------------------------------------
def compaction_event(before_tokens: int, after_tokens: int, dropped: int) -> dict:
    return {
        "type": "compaction",
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "dropped": dropped,
    }


# --- done / error / ask ------------------------------------------------------
def done_event(result: str, *, usage: dict | None = None) -> dict:
    event: dict = {"type": "done", "result": result}
    if usage is not None:
        event["usage"] = usage
    return event


def error_event(message: str) -> dict:
    return {"type": "error", "message": message}


def ask_event(
    id: str,
    kind: Literal["question", "plan", "continue"],
    question: str,
    options: list[dict],
    *,
    multi_select: bool = False,
    plan: str | None = None,
    header: str | None = None,
) -> dict:
    """Terminal event: the turn is suspended on an interrupt awaiting an answer.

    `options` is a list of `{"label": str, "description": str}`. The answer goes
    back via POST /resume with this event's `id`; the turn then continues from
    exactly where it paused (the checkpointer holds the state).
    """
    event: dict[str, Any] = {
        "type": "ask",
        "id": id,
        "kind": kind,
        "question": question,
        "options": options,
        "multi_select": multi_select,
    }
    if plan is not None:
        event["plan"] = plan
    if header is not None:
        event["header"] = header
    return event


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
