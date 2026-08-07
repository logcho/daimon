"""The reflection pass: trivial-turn gating, SKIP handling, and the run_turn
wiring that writes a note into memory after `done`."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from daimon_agent.graph import build_graph, close_checkpointer, make_sqlite_checkpointer
from daimon_agent.memory import MemoryStore
from daimon_agent.reflect import reflect_turn, should_reflect
from daimon_agent.run import run_turn

from fakes import FakeRouter, FakeTool, tool_call


def test_should_reflect_gates_on_tool_use() -> None:
    assert should_reflect("task", "done", [HumanMessage(content="hi")]) is False
    assert should_reflect("task", "", []) is False
    assert should_reflect("task", "Task complete.", []) is False
    assert should_reflect("task", "done", [ToolMessage(content="ok", tool_call_id="c1")]) is True


async def test_reflect_turn_skips_trivial() -> None:
    router = FakeRouter(pro_script=[AIMessage(content="SKIP")])
    assert await reflect_turn(router, "task", "result") is None


async def test_reflect_turn_returns_note() -> None:
    router = FakeRouter(pro_script=[AIMessage(content="Submit via the jobs portal: upload CV, then email the recruiter.")])
    note = await reflect_turn(router, "apply to Stripe", "Submitted.")
    assert "jobs portal" in note


async def test_run_turn_writes_reflect_note_into_memory(settings, tmp_path) -> None:
    checkpointer = await make_sqlite_checkpointer(tmp_path / "cp.db")
    memory = MemoryStore(tmp_path / "mem.db")
    router = FakeRouter(
        pro_script=[
            tool_call("fake", {}, id="call-1"),
            AIMessage(content="Submitted the application."),
            AIMessage(content="Submit via the jobs portal, then email the recruiter."),
        ]
    )
    tool = FakeTool(name="fake", responses=["ok"])
    graph = build_graph(settings, router, [tool], checkpointer=checkpointer)
    events: list[dict] = []

    try:
        result = await run_turn(
            "apply to Stripe",
            "reflect-session",
            settings,
            events.append,
            graph=graph,
            memory=memory,
            router=router,
        )
    finally:
        await close_checkpointer(checkpointer)

    assert result == "Submitted the application."
    assert [e["type"] for e in events][-1] == "done"
    # The reflection note is searchable in memory; the pro role did the review
    # (flash was never used).
    rows = memory.search_notes("jobs portal")
    assert len(rows) == 1
    assert rows[0]["filename"].startswith("reflect/")
    assert router._flash.calls == []
