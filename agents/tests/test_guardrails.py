"""Pure guardrail functions — the era-1 anti-loop machinery, unit-tested."""

from __future__ import annotations

import json

from daimon_agent.guardrails import (
    check_near_duplicate,
    check_page_unchanged,
    check_repeat,
    check_research_budget,
    jaccard,
)


def test_check_repeat_warns_only_on_second_identical_call() -> None:
    args = {"query": "best pizza dough recipe"}
    log: list[tuple[str, str]] = []
    assert check_repeat("web_search", args, log) is None
    log.append(("web_search", '{"query": "best pizza dough recipe"}'))
    warning = check_repeat("web_search", args, log)
    assert warning is not None
    assert "already called web_search" in warning
    # Same tool, different args: fine.
    assert check_repeat("web_search", {"query": "sourdough"}, log) is None
    # Same args, different tool: fine.
    assert check_repeat("read_page", args, log) is None


def test_check_research_budget_hard_stops_at_cap() -> None:
    assert check_research_budget("web_search", 9) is None
    warning = check_research_budget("web_search", 10)
    assert warning is not None
    assert "Hard stop" in warning
    assert "10-call" in warning
    # Non-research tools are never budgeted.
    assert check_research_budget("run_shell", 999) is None


def test_jaccard_math() -> None:
    assert jaccard("a b c", "a b c") == 1.0
    assert jaccard("a b c", "d e f") == 0.0
    assert jaccard("a b", "a b c") == 2 / 3
    assert jaccard("", "a b") == 0.0


def test_check_near_duplicate_warns_on_reworded_query() -> None:
    log = [("web_search", '{"query": "how to make pizza dough"}')]
    # Same intent, light rewording → word-set Jaccard >= 0.6.
    warning = check_near_duplicate("how to make pizza dough at home", log)
    assert warning is not None
    assert "too similar" in warning
    # Different topic entirely.
    assert check_near_duplicate("best running shoes 2026", log) is None
    # Exact same query is also caught by the repeat check; near-dup too.
    assert check_near_duplicate("how to make pizza dough", log) is not None


def test_check_page_unchanged_warns_only_on_same_content() -> None:
    text_a = "Page with stable content " * 10
    text_b = text_a + " and something new"
    warning, sig = check_page_unchanged(None, text_a)
    assert warning is None
    assert sig  # baseline established
    warning, sig2 = check_page_unchanged(sig, text_a)
    assert warning is not None
    assert "unchanged" in warning
    # Changed content resets the baseline without warning.
    warning, sig3 = check_page_unchanged(sig2, text_b)
    assert warning is None
    assert sig3 != sig2


# --- the repeat guard applies to every tool, not only research ----------------

def test_repeat_guard_covers_non_research_tools() -> None:
    """An agent stuck on an edit_file whose old_text never matches will reissue
    it verbatim until the step budget runs out. The check was always generic;
    only research tools were ever passed to it."""
    args = {"file_path": "a.py", "old_text": "x", "new_text": "y"}
    log = [("edit_file", json.dumps(args, sort_keys=True, default=str))]

    assert check_repeat("edit_file", args, log) is not None


def test_repeat_exempt_holds_the_tools_where_a_repeat_is_meaningless() -> None:
    from daimon_agent.guardrails import REPEAT_EXEMPT

    # Resubmitting the same todo list is a no-op, not a loop.
    assert "update_todos" in REPEAT_EXEMPT
    # Tools whose answer depends on the workspace must NOT be exempt — the
    # graph clears their log entries on mutation instead, which keeps the guard
    # while still allowing "fix, then re-run the suite".
    assert "run_shell" not in REPEAT_EXEMPT
    assert "read_file" not in REPEAT_EXEMPT


async def test_a_mutation_reopens_repeated_reads(settings) -> None:
    """read → write → read of the same path is the correct sequence, not a
    loop: the second read genuinely returns something different."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from daimon_agent.graph import build_graph, run_config

    from fakes import FakeRouter, FakeTool

    read_args = {"file_path": "a.py"}
    router = FakeRouter(
        pro_script=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": read_args, "id": "r1", "type": "tool_call"},
                    {"name": "write_file", "args": {"file_path": "a.py", "content": "new"},
                     "id": "w1", "type": "tool_call"},
                    {"name": "read_file", "args": read_args, "id": "r2", "type": "tool_call"},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    read = FakeTool(name="read_file", responses=["old contents", "new contents"])
    write = FakeTool(name="write_file", responses=["wrote it"])
    graph = build_graph(settings, router, [read, write])

    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("mut-1"))

    second = next(m for m in state["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "r2")
    assert second.content == "new contents"  # executed, not warned off
    assert len(read.calls) == 2


async def test_an_unchanged_repeat_is_still_warned(settings) -> None:
    """Without a mutation in between, the second identical call is a loop."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from daimon_agent.graph import build_graph, run_config

    from fakes import FakeRouter, FakeTool

    args = {"file_path": "a.py"}
    router = FakeRouter(
        pro_script=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": args, "id": "r1", "type": "tool_call"},
                    {"name": "read_file", "args": args, "id": "r2", "type": "tool_call"},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    read = FakeTool(name="read_file", responses=["contents", "contents"])
    graph = build_graph(settings, router, [read])

    state = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("rep-1"))

    second = next(m for m in state["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "r2")
    assert "already called read_file" in second.content
    assert len(read.calls) == 1  # the repeat never executed
