"""Conferring with the user: option parsing, the interrupt that suspends a
turn on `ask_user`/`present_plan`, and the resume that picks it back up.

The property that matters most here is that resuming doesn't re-run work.
LangGraph resumes an interrupt by re-executing the whole node, so the ask has
to be resolved before any tool in that batch has run — `test_resume_does_not_
rerun_tools_from_the_same_batch` is the guard on that.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from daimon_agent.graph import build_graph, close_checkpointer, make_sqlite_checkpointer, run_config
from daimon_agent.run import resume_turn, run_turn
from daimon_agent.tools.ask import parse_options

from fakes import FakeRouter, FakeTool, tool_call


# --- option parsing ----------------------------------------------------------

def test_parse_options_splits_label_and_description() -> None:
    options = parse_options("Postgres|Battle-tested\nSQLite|Zero setup")
    assert options == [
        {"label": "Postgres", "description": "Battle-tested"},
        {"label": "SQLite", "description": "Zero setup"},
    ]


def test_parse_options_tolerates_markdown_and_missing_descriptions() -> None:
    """A malformed options list should cost a plainer prompt, not a failed
    turn — the model writes markdown bullets out of habit."""
    assert parse_options("- Yes\n* No|not now\n\n  \n") == [
        {"label": "Yes", "description": ""},
        {"label": "No", "description": "not now"},
    ]


def test_parse_options_caps_the_list() -> None:
    assert len(parse_options("\n".join(f"opt{i}" for i in range(10)))) == 4


# --- the interrupt -----------------------------------------------------------

async def _graph_with_checkpointer(settings, router, tools, tmp_path, name="cp.db"):
    checkpointer = await make_sqlite_checkpointer(tmp_path / name)
    return build_graph(settings, router, tools, checkpointer=checkpointer), checkpointer


async def test_ask_user_suspends_the_turn_and_emits_an_ask_event(settings, tmp_path) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call(
                "ask_user",
                {"question": "Which database?", "options": "Postgres|Solid\nSQLite|Simple"},
                id="call-ask",
            ),
            AIMessage(content="Postgres it is."),
        ]
    )
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [], tmp_path)
    events: list[dict] = []
    try:
        result = await run_turn(
            "pick a db", "ask1", settings, events.append,
            graph=graph, capabilities=["ask"],
        )
    finally:
        await close_checkpointer(checkpointer)

    # The turn paused rather than finishing.
    assert result is None
    assert events[-1]["type"] == "ask"
    assert events[-1]["kind"] == "question"
    assert events[-1]["question"] == "Which database?"
    assert [o["label"] for o in events[-1]["options"]] == ["Postgres", "SQLite"]
    # No `done` — the terminal slot is exclusive.
    assert not any(e["type"] == "done" for e in events)
    # The model was called once; it has not yet seen an answer.
    assert len(router._pro.calls) == 1


async def test_resume_answers_and_finishes_the_turn(settings, tmp_path) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call("ask_user", {"question": "Which?", "options": "A|first"}, id="call-ask"),
            AIMessage(content="Going with A."),
        ]
    )
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [], tmp_path)
    try:
        first: list[dict] = []
        await run_turn("pick", "ask2", settings, first.append, graph=graph, capabilities=["ask"])
        ask = first[-1]

        second: list[dict] = []
        result = await resume_turn("A", "ask2", settings, second.append, graph=graph)
    finally:
        await close_checkpointer(checkpointer)

    assert result == "Going with A."
    assert second[-1]["type"] == "done"
    assert second[-1]["result"] == "Going with A."
    # The answer reached the model as the ask tool's result.
    answer_msgs = [
        m for m in router._pro.calls[-1] if isinstance(m, ToolMessage) and m.name == "ask_user"
    ]
    assert answer_msgs and "A" in answer_msgs[0].content
    assert ask["id"]  # the ask carried an id for the client to echo back


async def test_resume_does_not_rerun_tools_from_the_same_batch(settings, tmp_path) -> None:
    """Resuming re-executes the whole node. If the ask weren't resolved before
    the execution loop, a tool called alongside it would run a second time."""
    router = FakeRouter(
        pro_script=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "fake", "args": {}, "id": "c-tool", "type": "tool_call"},
                    {
                        "name": "ask_user",
                        "args": {"question": "OK?", "options": "Yes|go"},
                        "id": "c-ask",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="finished"),
        ]
    )
    tool = FakeTool(name="fake", responses=["ran once", "ran twice"])
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [tool], tmp_path)
    try:
        await run_turn("go", "ask3", settings, [].append, graph=graph, capabilities=["ask"])
        result = await resume_turn("Yes", "ask3", settings, [].append, graph=graph)
    finally:
        await close_checkpointer(checkpointer)

    assert result == "finished"
    assert len(tool.calls) == 1, "the tool ran again when the node re-entered"


async def test_present_plan_emits_a_plan_ask(settings, tmp_path) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call("present_plan", {"plan": "1. Do the thing\n2. Test it"}, id="call-plan"),
            AIMessage(content="Done."),
        ]
    )
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [], tmp_path)
    events: list[dict] = []
    try:
        await run_turn(
            "build it", "plan1", settings, events.append,
            graph=graph, capabilities=["ask"], mode="plan",
        )
    finally:
        await close_checkpointer(checkpointer)

    ask = events[-1]
    assert ask["type"] == "ask" and ask["kind"] == "plan"
    assert "Do the thing" in ask["plan"]
    # A default approve/revise choice when the model offered none.
    assert len(ask["options"]) == 2


async def test_plan_mode_reaches_the_system_prompt(settings, tmp_path) -> None:
    router = FakeRouter(pro_script=[AIMessage(content="ok")])
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [], tmp_path)
    try:
        await run_turn("go", "plan2", settings, [].append, graph=graph, mode="plan")
    finally:
        await close_checkpointer(checkpointer)

    prompt = str(router._pro.calls[0][0].content)
    assert "Plan mode is on" in prompt


# --- the plan gate -----------------------------------------------------------

async def test_plan_mode_blocks_mutating_tools_until_a_plan_is_approved(
    settings, tmp_path
) -> None:
    """The bug this exists for: told to plan first, the model went ahead and
    edited two files anyway. A prompt instruction is a request; this is a gate."""
    router = FakeRouter(
        pro_script=[
            tool_call("write_file", {"path": "a.py", "content": "x"}, id="c-write"),
            AIMessage(content="I should have planned first."),
        ]
    )
    tool = FakeTool(name="write_file", responses=["written"])
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [tool], tmp_path)
    try:
        await run_turn(
            "change it", "gate1", settings, [].append,
            graph=graph, capabilities=["ask"], mode="plan",
        )
    finally:
        await close_checkpointer(checkpointer)

    assert tool.calls == [], "a mutating tool ran without an approved plan"
    refusal = next(
        m for m in router._pro.calls[-1] if isinstance(m, ToolMessage) and m.name == "write_file"
    )
    assert "present_plan" in refusal.content


async def test_read_only_tools_still_work_in_plan_mode(settings, tmp_path) -> None:
    """Investigating is not changing anything — the gate would be useless if
    the agent couldn't research the plan it's meant to write."""
    router = FakeRouter(
        pro_script=[
            tool_call("read_file", {"path": "a.py"}, id="c-read"),
            AIMessage(content="read it"),
        ]
    )
    tool = FakeTool(name="read_file", responses=["contents"])
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [tool], tmp_path)
    try:
        await run_turn(
            "look at it", "gate2", settings, [].append,
            graph=graph, capabilities=["ask"], mode="plan",
        )
    finally:
        await close_checkpointer(checkpointer)
    assert len(tool.calls) == 1


async def test_approving_a_plan_unlocks_the_mutating_tools(settings, tmp_path) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call("present_plan", {"plan": "1. Write a.py"}, id="c-plan"),
            tool_call("write_file", {"path": "a.py", "content": "x"}, id="c-write"),
            AIMessage(content="done"),
        ]
    )
    tool = FakeTool(name="write_file", responses=["written"])
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [tool], tmp_path)
    try:
        await run_turn(
            "change it", "gate3", settings, [].append,
            graph=graph, capabilities=["ask"], mode="plan",
        )
        result = await resume_turn("Go ahead", "gate3", settings, [].append, graph=graph)
    finally:
        await close_checkpointer(checkpointer)

    assert result == "done"
    assert len(tool.calls) == 1


async def test_rejecting_a_plan_keeps_the_gate_shut(settings, tmp_path) -> None:
    """"Revise it" is not approval — a revised plan earns its own answer."""
    router = FakeRouter(
        pro_script=[
            tool_call("present_plan", {"plan": "1. Write a.py"}, id="c-plan"),
            tool_call("write_file", {"path": "a.py", "content": "x"}, id="c-write"),
            AIMessage(content="ok, revising"),
        ]
    )
    tool = FakeTool(name="write_file", responses=["written"])
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [tool], tmp_path)
    try:
        await run_turn(
            "change it", "gate4", settings, [].append,
            graph=graph, capabilities=["ask"], mode="plan",
        )
        await resume_turn("Revise it", "gate4", settings, [].append, graph=graph)
    finally:
        await close_checkpointer(checkpointer)
    assert tool.calls == []


async def test_normal_mode_does_not_gate_anything(settings, tmp_path) -> None:
    router = FakeRouter(
        pro_script=[
            tool_call("write_file", {"path": "a.py", "content": "x"}, id="c-write"),
            AIMessage(content="done"),
        ]
    )
    tool = FakeTool(name="write_file", responses=["written"])
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [tool], tmp_path)
    try:
        await run_turn("change it", "gate5", settings, [].append, graph=graph)
    finally:
        await close_checkpointer(checkpointer)
    assert len(tool.calls) == 1


# --- the capability gate -----------------------------------------------------

async def test_ask_without_the_capability_does_not_suspend(settings, tmp_path) -> None:
    """A client that can't answer never gets asked. The model shouldn't be
    able to call the tool at all — it isn't bound — but if it names one
    anyway, the turn must finish with an instruction to decide rather than
    suspend on a question nobody is listening for."""
    router = FakeRouter(
        pro_script=[
            tool_call("ask_user", {"question": "Which?", "options": "A|first"}, id="call-ask"),
            AIMessage(content="I picked A and moved on."),
        ]
    )
    graph, checkpointer = await _graph_with_checkpointer(settings, router, [], tmp_path)
    events: list[dict] = []
    try:
        result = await run_turn(
            "go", "nocap", settings, events.append, graph=graph, capabilities=[]
        )
    finally:
        await close_checkpointer(checkpointer)

    assert result == "I picked A and moved on."
    assert events[-1]["type"] == "done"
    assert not any(e["type"] == "ask" for e in events)
    refusal = next(
        m for m in router._pro.calls[-1] if isinstance(m, ToolMessage) and m.name == "ask_user"
    )
    assert "not available here" in refusal.content


async def test_ask_tools_are_not_bound_without_the_capability(settings, tmp_path) -> None:
    """The gate proper: the schema never reaches the model in the first place."""
    bound: list[list[str]] = []

    class RecordingModel(FakeRouter().pro().__class__):  # ScriptedModel subclass
        def bind_tools(self, tools, **kwargs):
            bound.append([getattr(t, "name", "") for t in tools])
            return self

    router = FakeRouter(pro_script=[AIMessage(content="ok")])
    router._pro = RecordingModel([AIMessage(content="ok")])
    tools = [FakeTool(name="ask_user"), FakeTool(name="present_plan"), FakeTool(name="fake")]
    graph, checkpointer = await _graph_with_checkpointer(settings, router, tools, tmp_path)
    try:
        await run_turn("go", "bind1", settings, [].append, graph=graph, capabilities=[])
    finally:
        await close_checkpointer(checkpointer)
    assert bound[0] == ["fake"]

    bound.clear()
    router._pro = RecordingModel([AIMessage(content="ok")])
    graph2, checkpointer2 = await _graph_with_checkpointer(
        settings, router, tools, tmp_path, name="cp2.db"
    )
    try:
        await run_turn("go", "bind2", settings, [].append, graph=graph2, capabilities=["ask"])
    finally:
        await close_checkpointer(checkpointer2)
    assert set(bound[0]) == {"ask_user", "present_plan", "fake"}


async def test_interrupt_state_survives_a_new_graph_object(settings, tmp_path) -> None:
    """The pause lives in the checkpointer, not in memory — so answering after
    a restart works, which is the reason for using interrupt at all."""
    router = FakeRouter(
        pro_script=[
            tool_call("ask_user", {"question": "Which?", "options": "A|first"}, id="call-ask"),
            AIMessage(content="picked"),
        ]
    )
    checkpointer = await make_sqlite_checkpointer(tmp_path / "shared.db")
    try:
        first = build_graph(settings, router, [], checkpointer=checkpointer)
        await run_turn("pick", "restart", settings, [].append, graph=first, capabilities=["ask"])

        # A different compiled graph over the same checkpointer.
        second = build_graph(settings, router, [], checkpointer=checkpointer)
        state = await second.ainvoke(Command(resume="A"), run_config("restart"))
    finally:
        await close_checkpointer(checkpointer)

    assert any(
        isinstance(m, ToolMessage) and m.name == "ask_user" for m in state["messages"]
    )
