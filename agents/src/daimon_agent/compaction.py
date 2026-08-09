"""Token-threshold compaction — the era-1 rollback hack's replacement.

When the conversation (sans system prompt) exceeds the threshold, a flash call
summarizes everything but the most recent messages, and the summary re-enters
as a SystemMessage ahead of them. Flash does the summarizing — the hybrid
routing rule: cheap, high-volume calls never burn the Pro role.

The threshold is measured in *real* tokens, taken from the last model call's
usage. Character count is only the cold-start estimate, used before any usage
has been reported for the session — it was the whole measure once, and a
40k-character guess is a poor stand-in for a context window quoted in tokens.

The result is written back into graph state (see `CompactionResult.as_update`),
so the checkpointer stores the compacted history. Compaction previously ran as
a per-call transformation that was never persisted, which meant every
subsequent hop past the threshold re-summarized the same prefix — N model calls
for one long turn. Crash safety is preserved because the whole thing is a
single atomic node return: either the summarized history commits or the
original stands, never a half-applied edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AnyMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from .usage import get_active_usage

# How many of the most recent messages survive verbatim under a summary.
KEEP_LAST = 6

#: Rough chars-per-token for the cold-start estimate (Chinese/English mix).
CHARS_PER_TOKEN = 3.5

_SUMMARY_PROMPT = """You are summarizing a conversation for the assistant's context window.

Summarize everything up to the most recent messages in one compact paragraph,
preserving: the user's goal, facts established, tools used, and results that
are still needed. Omit what is no longer relevant.

CONVERSATION SO FAR:
{history}

SUMMARY:
"""


def estimate_size(messages: list[AnyMessage]) -> int:
    """Rough size in characters. Deterministic and testable without a model."""
    return sum(len(str(getattr(m, "content", ""))) for m in messages)


def estimate_tokens(messages: list[AnyMessage]) -> int:
    """Token estimate from character count — the fallback when no model call
    has reported real usage yet."""
    return int(estimate_size(messages) / CHARS_PER_TOKEN)


def context_tokens(messages: list[AnyMessage]) -> int:
    """The conversation's size in tokens: the last call's reported input count
    when we have one, else the character estimate.

    The reported number is what the provider actually billed for the prompt, so
    it accounts for tool schemas and the system prompt that the message list
    alone doesn't show.
    """
    acc = get_active_usage()
    if acc is not None and acc.context_tokens > 0:
        return acc.context_tokens
    return estimate_tokens(messages)


def should_compact(messages: list[AnyMessage], threshold_tokens: int) -> bool:
    return context_tokens(messages) > threshold_tokens


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


@dataclass
class CompactionResult:
    """What compaction produced: the message list to send to the model *and*
    the state update that persists it."""

    #: The compacted conversation, for this model call.
    messages: list[AnyMessage] = field(default_factory=list)
    #: The replacement history to persist (summary + kept suffix).
    replacement: list[AnyMessage] = field(default_factory=list)
    before_tokens: int = 0
    after_tokens: int = 0
    dropped: int = 0

    def as_update(self) -> list[Any]:
        """Messages-channel update that swaps the whole history for the
        compacted one. `REMOVE_ALL_MESSAGES` then re-add is LangGraph's
        idiom for a wholesale rewrite — a per-id removal list would have to
        stay in sync with whatever the reducer assigned ids to."""
        return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *self.replacement]


async def compact(
    router: Any, messages: list[AnyMessage], *, before_tokens: int = 0
) -> CompactionResult | None:
    """Summarize the old tail with flash; keep the recent messages verbatim.
    Returns None when the summary came back empty — better to carry a long
    context than to drop history for nothing."""
    summary = await router.flash().ainvoke(
        _SUMMARY_PROMPT.format(history=_history_block(messages))
    )
    # The summarizer is a real model call and costs real money; count it.
    from .usage import extract_usage, record

    record(extract_usage(summary, fallback_model=router.model_name("flash")))

    summary_text = str(summary.content).strip()
    if not summary_text:
        return None
    kept = _compact_keep(messages)
    replacement = [
        SystemMessage(content=f"Earlier conversation summary: {summary_text}"),
        *kept,
    ]
    return CompactionResult(
        messages=list(replacement),
        replacement=replacement,
        before_tokens=before_tokens or estimate_tokens(messages),
        after_tokens=estimate_tokens(replacement),
        dropped=len(messages) - len(kept),
    )


async def compact_if_needed(
    settings: Any, router: Any, messages: list[AnyMessage]
) -> CompactionResult | None:
    """The agent-node entry point: None under the threshold, else a result."""
    size = context_tokens(messages)
    if size <= settings.compaction_tokens:
        return None
    return await compact(router, messages, before_tokens=size)
