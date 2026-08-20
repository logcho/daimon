"""Scoring: verifiable checks without any API, the judge role with a
scripted model, and the pass gate."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from daimon_agent.optimizer.scorer import (
    JUDGE_THRESHOLD,
    all_checks_pass,
    llm_judge,
    run_checks,
    score,
)

from fakes import FakeRouter

TASK = {"instruction": "make a changelog", "rubric": "Categorized changelog with headings."}


def test_run_checks_contains_and_regex() -> None:
    checks = [
        {"kind": "contains", "value": "## "},
        {"kind": "contains", "value": "Missing thing"},
        {"kind": "regex", "value": r"v?\d+\.\d+"},
    ]
    details = run_checks("## v1.2\n\nFixed a bug.", checks)
    assert [d["passed"] for d in details] == [True, False, True]
    assert not all_checks_pass(details)


def test_run_checks_invalid_regex_is_a_failed_check_not_a_crash() -> None:
    details = run_checks("anything", [{"kind": "regex", "value": "([unclosed"}])
    assert details[0]["passed"] is False


def test_run_checks_exit_code_uses_cwd(tmp_path) -> None:
    (tmp_path / "marker.txt").write_text("here")
    ok = run_checks("", [{"kind": "exit_code", "value": "test -f marker.txt"}], cwd=tmp_path)
    bad = run_checks("", [{"kind": "exit_code", "value": "test -f absent.txt"}], cwd=tmp_path)
    assert ok[0]["passed"] is True
    assert bad[0]["passed"] is False


def test_run_checks_unknown_kind_fails_cleanly() -> None:
    details = run_checks("x", [{"kind": "teleport", "value": "y"}])
    assert details[0]["passed"] is False


async def test_llm_judge_parses_score() -> None:
    router = FakeRouter(judge_script=[AIMessage(content="Score: 4")])
    assert await llm_judge(router, TASK, "some answer") == 4


async def test_llm_judge_garbage_scores_one() -> None:
    router = FakeRouter(judge_script=[AIMessage(content="I liked it a lot")])
    assert await llm_judge(router, TASK, "some answer") == 1


async def test_score_gates_on_both_checks_and_judge() -> None:
    task = {
        "instruction": "make a changelog",
        "rubric": "Categorized changelog with headings.",
        "checks": [{"kind": "contains", "value": "## "}],
    }
    # Checks fail -> not passed even with a strong judge.
    router = FakeRouter(judge_script=[AIMessage(content="Score: 5")])
    bad = await score(router, task, "no heading here")
    assert bad["checks_passed"] is False
    assert bad["passed"] is False

    # Checks pass but the judge is below the threshold -> not passed.
    router = FakeRouter(judge_script=[AIMessage(content="Score: 3")])
    low = await score(router, task, "## heading\n\nbody")
    assert low["checks_passed"] is True
    assert low["llm_score"] < JUDGE_THRESHOLD
    assert low["passed"] is False

    # Both pass -> passed.
    router = FakeRouter(judge_script=[AIMessage(content="Score: 5")])
    good = await score(router, task, "## heading\n\nbody")
    assert good["passed"] is True
