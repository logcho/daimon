"""Token-threshold compaction — the era-1 rollback hack's replacement.

When the conversation (sans system prompt) exceeds the threshold, a flash
call summarizes everything but the most recent messages, and the summary
re-enters as a SystemMessage ahead of them. Crash-safe by construction: the
checkpointer keeps the full history, so a crashed summarization simply runs
again next turn. Flash does the summarizing — the hybrid routing rule: cheap,
high-volume calls never burn the Pro role.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AnyMessage, SystemMessage

# How many of the most recent messages survive verbatim under a summary.
KEEP_LAST = 6

_SUMMARY_PROMPT = """You are summarizing a conversation for the assistant's context window.

Summarize everything up to the most recent messages in one compact paragraph,
preserving: the user's goal, facts established, tools used, and results that
are still needed. Omit what is no longer relevant.

CONVERSATION SO FAR:
{history}

SUMMARY:
"""


def estimate_size(messages: list[AnyMessage]) -> int:
    """Rough size in characters — a cheap proxy for tokens (Chinese/English
    mix, ~3-4 chars per token). Deterministic and testable without a model."""
    return sum(len(str(getattr(m, "content", ""))) for m in messages)


def should_compact(messages: list[AnyMessage], threshold_chars: int) -> bool:
    return estimate_size(messages) > threshold_chars


def _history_block(messages: list[AnyMessage]) -> str:
    lines = []
    for m in messages[:-KEEP_LAST]:
        role = getattr(m, "type", "message")
        content = str(getattr(m, "content", ""))
        lines.append(f"{role}: {content[:2000]}")
    return "\n".join(lines) or "(nothing yet)"


def _compact_keep(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Return the verbatim suffix to keep after summarisation, extended
    backward as needed so no ToolMessage is orphaned from its AIMessage with
    tool_calls — DeepSeek rejects orphaned tool-result messages."""
    kept = list(messages[-KEEP_LAST:])

    # Collect tool_call_ids from ToolMessages in the kept suffix.
    needed_ids: set[str] = set()
    for m in kept:
        tc_id = getattr(m, "tool_call_id", None)
        if tc_id:
            needed_ids.add(tc_id)

    # Walk backward through the summarised prefix and pull in any AIMessage
    # whose tool_calls are referenced by the kept ToolMessages.
    for m in reversed(messages[:-KEEP_LAST]):
        if not needed_ids:
            break
        tc_list = getattr(m, "tool_calls", None)
        if tc_list:
            tc_ids = {tc["id"] for tc in tc_list if "id" in tc}
            if tc_ids & needed_ids:
                needed_ids -= tc_ids
                kept.insert(0, m)

    return kept


async def compact(router: Any, messages: list[AnyMessage]) -> list[AnyMessage]:
    """Summarize the old tail with flash; keep the recent messages verbatim."""
    summary = await router.flash().ainvoke(
        _SUMMARY_PROMPT.format(history=_history_block(messages))
    )
    summary_text = str(summary.content).strip()
    if not summary_text:
        return list(messages)
    kept = _compact_keep(messages)
    return [SystemMessage(content=f"Earlier conversation summary: {summary_text}"), *kept]


async def compact_if_needed(
    settings: Any, router: Any, messages: list[AnyMessage]
) -> list[AnyMessage]:
    """The agent-node entry point: no-op under the threshold, else compact."""
    if not should_compact(messages, settings.compaction_chars):
        return list(messages)
    return await compact(router, messages)
