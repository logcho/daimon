"""The reflection pass — a post-turn evaluation that runs after `done`.

Gated: only turns that actually used tools are worth reflecting on (a pure
chat exchange is trivially complete). The Pro model reviews the completed
task and, when a reusable procedure emerged, produces a vault note that
memory.index_note makes searchable for future turns. SKIP keeps the noise
down. Reflect runs after the done event is emitted — the user's result is
never delayed by it — and writes only to memory, never to the UI stream.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage

_REFLECT_PROMPT = """You are the reflection pass for a completed task. Review the task and its
result and decide whether it contains a procedure worth remembering.

TASK: {instruction}

RESULT: {result}

A reusable procedure is a multi-step approach that would help with a similar
future task (how to submit a job application, how to release a package, how a
specific page's flow works). A one-off fact ("the sky is blue", "2+2 is 4",
an error that resolved itself) is NOT.

If there is a reusable procedure, reply with the note text only — one short
paragraph, imperative, what to do. If the task was trivial or one-off, reply
with exactly: SKIP
"""


def should_reflect(instruction: str, result: str | None, messages: list[Any]) -> bool:
    """The trivial-turn gate: no tool use means nothing was learned that the
    conversation doesn't already show. Errors and empty results never reflect."""
    if not result or not result.strip():
        return False
    if result.strip() == "Task complete.":
        return False
    return any(isinstance(m, ToolMessage) for m in messages)


async def reflect_turn(router: Any, instruction: str, result: str) -> str | None:
    """The Pro-model review. Returns the note text, or None for SKIP / an
    empty answer."""
    prompt = _REFLECT_PROMPT.format(instruction=instruction, result=result)
    response = await router.pro().ainvoke(prompt)
    text = str(response.content).strip()
    if not text or text.upper().startswith("SKIP"):
        return None
    return text
