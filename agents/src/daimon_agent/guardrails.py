"""Anti-loop guardrails, relocated from era-1's tool closures into pure
functions the graph's tools node applies (the era-1 lesson: guardrails live
in the graph, not scattered globals). Each returns a warning string to feed
back to the model as a tool result, or None to allow the call.

The era-1 system prompt told the model to *believe* these warnings and change
approach — that text survives in prompts.py so the model cooperates with
them.
"""

from __future__ import annotations

import hashlib
import json

# web_search, open_url, read_page, web_fetch, and research share one hard
# budget per turn. The research fan-out counts as one call even though the
# subagents it spawns do their own (subgraph-bounded) web work.
RESEARCH_TOOLS = frozenset({"web_search", "open_url", "read_page", "web_fetch", "research"})
RESEARCH_TOOL_BUDGET = 10
NEAR_DUPLICATE_THRESHOLD = 0.6

REPEAT_WARNING = (
    "You already called {tool} with the exact same arguments this turn. Repeating it will not "
    "produce new information. Believe this warning and change approach — try a different query, "
    "a different page, or stop and report what you have so far."
)

BUDGET_WARNING = (
    "Hard stop: you have used your {budget}-call research budget "
    "(web_search/open_url/read_page/web_fetch) for this turn. These tools will "
    "not run again until the next turn. Believe this warning and finish from "
    "what you already have, or tell the user what remains to be found."
)

NEAR_DUPLICATE_WARNING = (
    'You already searched for something too similar to "{prev}" this turn. '
    "Repeating the same search reworded will not produce new information. Believe this warning "
    "and change approach — a different query angle, or stop and report what you have."
)

PAGE_UNCHANGED_WARNING = (
    "The page you just read is unchanged since your last read_page this turn — reading it again "
    "will not produce new information. Believe this warning: only re-read after a click, "
    "fill_field, or navigation has actually changed the page."
)


def _args_signature(args: dict) -> str:
    return json.dumps(args, sort_keys=True, default=str)


def check_repeat(tool: str, args: dict, call_log: list[tuple[str, str]]) -> str | None:
    """Warn when the exact (tool, args) pair was already executed this turn."""
    sig = _args_signature(args)
    for prev_tool, prev_sig in call_log:
        if prev_tool == tool and prev_sig == sig:
            return REPEAT_WARNING.format(tool=tool)
    return None


def check_research_budget(tool: str, research_used: int, budget: int = RESEARCH_TOOL_BUDGET) -> str | None:
    """Hard-stop once the per-turn research budget is exhausted."""
    if tool in RESEARCH_TOOLS and research_used >= budget:
        return BUDGET_WARNING.format(budget=budget)
    return None


def jaccard(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def check_near_duplicate(query: str, call_log: list[tuple[str, str]]) -> str | None:
    """Warn when a web_search query is too similar (word-set Jaccard >= 0.6)
    to one already run this turn."""
    for prev_tool, prev_sig in call_log:
        if prev_tool != "web_search":
            continue
        try:
            prev_query = json.loads(prev_sig).get("query")
        except (ValueError, AttributeError):
            continue
        if prev_query and jaccard(query, prev_query) >= NEAR_DUPLICATE_THRESHOLD:
            return NEAR_DUPLICATE_WARNING.format(prev=prev_query)
    return None


def page_signature(text: str) -> str:
    """Signature of a read_page result; equal signatures mean unchanged page."""
    return hashlib.sha1(text[:2000].encode("utf-8", errors="replace")).hexdigest()


def check_page_unchanged(previous_signature: str | None, text: str) -> tuple[str | None, str]:
    """Returns (warning, new_signature). The warning fires only when the page
    content matches the last read this turn; the signature is always updated
    so a genuinely new read resets the baseline."""
    new_sig = page_signature(text)
    if previous_signature is not None and new_sig == previous_signature:
        return PAGE_UNCHANGED_WARNING, new_sig
    return None, new_sig
