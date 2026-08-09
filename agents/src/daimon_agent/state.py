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
    # (call_id, sub_id, tool_name, query, agent_type) — fan-out requests
    # stashed by the tools node, drained concurrently by the subagents node.
    # sub_id is a unique step-id per query so a UI can track each sub-agent's
    # spawn→done transitions independently while they run at the same time.
    research_pending: list[tuple[str, str, str, str, str]]
    # What the client can render. "ask" gates the ask_user/present_plan tools:
    # the agent is only offered them when somebody is there to answer.
    capabilities: list[str]
    # "plan" makes the agent present a plan and wait for approval before it
    # changes anything; "normal" is the default.
    mode: str
    # Set once the user approves a present_plan in this turn. Until then the
    # tools node refuses every mutating tool — the gate is structural rather
    # than a prompt instruction, because by the time you notice a model
    # ignored "ask first", it has already written the file.
    plan_approved: bool
    # Set on a sub-agent's own state so its events can be attributed to it
    # while several run concurrently. Absent on the main agent.
    agent_id: str
