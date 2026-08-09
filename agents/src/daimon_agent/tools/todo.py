"""The agent's visible task list.

A multi-step task the user can't see the shape of is a black box: they get
tool calls scrolling past and no idea how much is left. The todo list is the
cheapest fix — the agent writes down its plan, marks items off as it goes, and
the UI renders the checklist live.

State is per session and lives only in this process. It is deliberately not
checkpointed: a todo list is a view of the *current* turn's intent, and a stale
one restored from disk three days later would be worse than none. Every write
emits the whole list rather than a patch, so a UI that missed an event still
converges.
"""

from __future__ import annotations

from ..emitter import emit
from ..events import todo_event

VALID_STATUSES = ("pending", "in_progress", "done")

#: session_id → the list, newest write wins.
_lists: dict[str, list[dict]] = {}

_STATUS_ALIASES = {
    "todo": "pending",
    "pending": "pending",
    "doing": "in_progress",
    "active": "in_progress",
    "in_progress": "in_progress",
    "in-progress": "in_progress",
    "wip": "in_progress",
    "done": "done",
    "complete": "done",
    "completed": "done",
    "x": "done",
}


def parse_todos(raw: str) -> list[dict]:
    """Parse `status|text` lines into todo items.

    Accepts markdown checkboxes too (`- [x] ship it`), because a model asked
    for a task list writes one of those about as often as it follows the
    format. Unrecognised status words fall back to pending rather than failing
    the call — a slightly wrong checklist still beats an error.
    """
    items: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line[:2] in ("- ", "* "):
            line = line[2:].strip()

        status = "pending"
        if line[:3].lower() in ("[x]", "[✓]"):
            status, line = "done", line[3:].strip()
        elif line[:3] == "[ ]":
            status, line = "pending", line[3:].strip()
        elif "|" in line:
            head, _, tail = line.partition("|")
            key = head.strip().lower()
            if key in _STATUS_ALIASES:
                status, line = _STATUS_ALIASES[key], tail.strip()

        if not line:
            continue
        items.append({"id": str(len(items) + 1), "text": line[:200], "status": status})
    return items


def set_todos(session_id: str, raw: str) -> str:
    """Replace the session's list and broadcast it. Returns the confirmation
    the model sees — a compact rendering of what it just wrote, so a wrong
    parse is visible to the agent, not only to the user."""
    items = parse_todos(raw)
    _lists[session_id] = items
    emit(todo_event(items))
    if not items:
        return "Todo list cleared."

    in_progress = [i for i in items if i["status"] == "in_progress"]
    done = sum(1 for i in items if i["status"] == "done")
    lines = [
        f"Todo list updated ({done}/{len(items)} done):",
        *(
            f"  [{'x' if i['status'] == 'done' else '~' if i['status'] == 'in_progress' else ' '}] {i['text']}"
            for i in items
        ),
    ]
    if len(in_progress) > 1:
        lines.append(
            "Note: more than one item is in_progress. Keep exactly one active "
            "so the user can see what you're on right now."
        )
    return "\n".join(lines)


def get_todos(session_id: str) -> list[dict]:
    return list(_lists.get(session_id, []))


def clear_todos(session_id: str) -> None:
    _lists.pop(session_id, None)


UPDATE_TODOS_DESCRIPTION = (
    "Write or update your task list for this piece of work — the user sees it "
    "as a live checklist. Use it for anything with three or more real steps, "
    "and skip it for single-step work where it would be noise. Send the WHOLE "
    "list every time (it replaces the previous one), one item per line as "
    "'status|task', where status is pending, in_progress, or done. Keep exactly "
    "one item in_progress, and mark an item done as soon as it is done rather "
    "than batching updates at the end."
)
