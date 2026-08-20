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

from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from .usage import get_active_usage

# How many of the most recent messages survive verbatim under a summary.
# Twelve, not six: a coding hop is an assistant message plus its tool results,
# so six covered barely two rounds of work and cut the ground out from under
# whatever the agent was in the middle of.
KEEP_LAST = 12

#: Rough chars-per-token for the cold-start estimate (Chinese/English mix).
CHARS_PER_TOKEN = 3.5

#: A paragraph is the wrong shape for this. What replaces the dropped history
#: is not a description of the conversation — it is the working state an agent
#: needs to carry on: what it is doing, what it already decided, which files it
#: touched, what is still open. Headed sections make each of those survivable
#: on its own; prose lets the model compress away the file paths first.
_SUMMARY_PROMPT = """You are compacting a working session so the assistant can carry on without \
re-reading it. This is a handover, not a description.

Write these sections, omitting any that genuinely has no content. Be specific — \
file paths, function names, commands, and exact values are the whole point. Do \
not summarize away an identifier.

## Goal
What the user is ultimately trying to achieve, in their terms.

## Decisions made
Choices already settled, and why. These must not be relitigated.

## Files and state touched
Every file read or changed, by path, with what changed about it. Also any \
kernel/shell state established.

## Findings
Facts established that the assistant would otherwise have to rediscover.

## Open threads
What is unfinished, what failed, what was deliberately deferred.

## Immediate next step
The single thing to do next.

CONVERSATION SO FAR:
{history}

HANDOVER:
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


def projected_tokens(messages: list[AnyMessage]) -> int:
    """The size of the request we are *about* to send.

    `context_tokens` is what the provider billed for the last one, which is the
    conversation as it stood one hop ago — it cannot see the assistant message
    and tool results appended since. Compacting on that number means the first
    over-threshold request always goes out anyway and the trigger only fires
    once it comes back, which on a hop that reads three large files is exactly
    the request you wanted to avoid.

    So: take the measured number and add a character estimate of everything
    appended since. Estimates are only ever used for the delta, never for the
    stable prefix, which is where they were always weakest.
    """
    acc = get_active_usage()
    if acc is None or acc.context_tokens <= 0:
        return estimate_tokens(messages)
    fresh = messages[acc.context_messages :] if acc.context_messages else []
    return acc.context_tokens + estimate_tokens(fresh)


def should_compact(messages: list[AnyMessage], threshold_tokens: int) -> bool:
    return projected_tokens(messages) > threshold_tokens


def _history_block(dropped: list[AnyMessage]) -> str:
    """The messages being summarized, rendered for the summarizer.

    Takes the dropped list explicitly rather than re-deriving it from
    KEEP_LAST: `_compact_keep` extends the kept window backward for tool
    pairing and for the newest instruction, so the two no longer agree, and a
    message that is both summarized and kept is wasted budget.
    """
    lines = []
    for m in dropped:
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

    # The instruction being worked on survives verbatim, however long the tool
    # loop has run. On a long turn it falls out of the window early, and
    # "the user asked about the parser" is a poor stand-in for the words they
    # actually used — those are what the rest of the turn is measured against.
    if not any(isinstance(m, HumanMessage) for m in kept):
        for m in reversed(messages[:-KEEP_LAST]):
            if isinstance(m, HumanMessage):
                # Ahead of any AIMessage pulled in above: that message answers
                # this instruction, so it comes after it.
                kept.insert(0, m)
                break

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
    kept = _compact_keep(messages)
    kept_ids = {id(m) for m in kept}
    dropped = [m for m in messages if id(m) not in kept_ids]
    summary = await router.flash().ainvoke(
        _SUMMARY_PROMPT.format(history=_history_block(dropped))
    )
    # The summarizer is a real model call and costs real money; count it.
    from .usage import extract_usage, record

    record(extract_usage(summary, fallback_model=router.model_name("flash")))

    summary_text = str(summary.content).strip()
    if not summary_text:
        return None
    replacement = [
        SystemMessage(
            content=(
                "Handover from the earlier part of this session, which has been "
                f"compacted away:\n\n{summary_text}"
            )
        ),
        *kept,
    ]
    return CompactionResult(
        messages=list(replacement),
        replacement=replacement,
        before_tokens=before_tokens or estimate_tokens(messages),
        after_tokens=estimate_tokens(replacement),
        dropped=len(dropped),
    )


async def compact_if_needed(
    settings: Any, router: Any, messages: list[AnyMessage]
) -> CompactionResult | None:
    """The agent-node entry point: None under the threshold, else a result."""
    size = projected_tokens(messages)
    if size <= settings.compaction_tokens:
        return None
    return await compact(router, messages, before_tokens=size)
