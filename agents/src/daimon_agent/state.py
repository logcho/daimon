"""Graph state. `messages` is the checkpointer-owned conversation; everything
else is per-turn guardrail bookkeeping the graph nodes own (the era-1 lesson:
no module-global conversation array, no hand-rolled rollback)."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    instruction: str
    session_id: str
    # Per-turn web_search + open_url + read_page + web_fetch budget counter
    # (era-1 RESEARCH_TOOL_BUDGET). Reset to 0 by run.py at the top of each
    # turn so a new message always gets a fresh budget.
    research_used: int
    # (tool_name, json-sorted args) — exact-repeat and near-duplicate inputs.
    call_log: list[tuple[str, str]]
    # sha1 of the last read_page content — unchanged-page detection.
    last_read_signature: str | None
    # Injected skills tail (see skills/injector.py) — part of the prompt, not
    # the conversation. Passed per-run so rollouts can vary only the skill.
    skills_block: str
    # (call_id, tool_name, query) — research fan-out requests stashed by the
    # tools node, drained by the subagents node (Phase E).
    research_pending: list[tuple[str, str, str]]
