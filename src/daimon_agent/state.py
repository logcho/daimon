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
    # web_search + open_url + read_page budget counter (era-1 RESEARCH_TOOL_BUDGET).
    research_used: int
    # (tool_name, json-sorted args) — exact-repeat and near-duplicate inputs.
    call_log: list[tuple[str, str]]
    # sha1 of the last read_page content — unchanged-page detection.
    last_read_signature: str | None
