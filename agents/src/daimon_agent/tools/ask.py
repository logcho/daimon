"""Asking the user — the two tools that let the agent stop and confer.

Neither tool has a body worth speaking of. The graph intercepts them in
`tools_node` and resolves them with LangGraph's `interrupt()`, because the
suspension has to happen at a point where re-entry is safe (see the comment
there). What lives here is the shared vocabulary: which names count as ask
tools, and how an options list is spelled.

Options ride in a single flat string — one option per line, `label|description`
— for the same reason every other tool schema here is flat strings: nested
object schemas are where DeepSeek's tool calling starts producing malformed
arguments.
"""

from __future__ import annotations

#: Tool names the graph resolves via interrupt rather than execution.
ASK_TOOLS = frozenset({"ask_user", "present_plan"})

MAX_OPTIONS = 4
_MAX_LABEL = 60
_MAX_DESCRIPTION = 240


def parse_options(raw: str) -> list[dict]:
    """Parse `label|description` lines into option dicts.

    Tolerant by design: a line with no `|` becomes a label with no description,
    blank lines are skipped, and anything past the cap is dropped. A malformed
    options list should cost the user a worse-looking prompt, not a failed turn.
    """
    options: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip list markers the model may add out of markdown habit.
        if line[:2] in ("- ", "* "):
            line = line[2:].strip()
        label, sep, description = line.partition("|")
        label = label.strip()[:_MAX_LABEL]
        if not label:
            continue
        options.append(
            {
                "label": label,
                "description": (description.strip() if sep else "")[:_MAX_DESCRIPTION],
            }
        )
        if len(options) >= MAX_OPTIONS:
            break
    return options


ASK_USER_DESCRIPTION = (
    "Ask the user a question and wait for their answer. Use this when the task "
    "is genuinely ambiguous and different readings would lead to materially "
    "different work — not for confirmation of something you can decide "
    "yourself, and not for anything you could find out by reading the project. "
    "Offer 2-4 concrete options, best first. `options` is one option per line "
    "as 'label|short description'. Set multi_select to 'true' when several "
    "answers can be picked together. The turn pauses until they answer."
)

PRESENT_PLAN_DESCRIPTION = (
    "Show the user your plan and wait for approval before you start changing "
    "anything. `plan` is markdown: what you'll do, which files you'll touch, "
    "and anything you're assuming. Keep it scannable — a short list beats "
    "paragraphs. `options` is one option per line as 'label|short description'; "
    "leave it empty for a plain approve/revise choice. Required before any "
    "edit when the session is in plan mode."
)
