"""Pure guardrail functions — the era-1 anti-loop machinery, unit-tested."""

from __future__ import annotations

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
