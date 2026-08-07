"""Scoring a rollout result — SkillOpt's two-part evaluation.

1. Verifiable checks (pure, no API): `contains` / `regex` / `exit_code`
   assertions against the result text. Deterministic and cheap.
2. LLM judge (router.judge(), temperature 0.7): a 1–5 rubric score from a
   separate model instance — the SkillOpt rule that the optimizer is its own
   role, never the agent that produced the work.

A task passes when every verifiable check passes AND the judge scores >= 4.
The strict held-out gate in optimizer.py decides acceptance on these.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

JUDGE_THRESHOLD = 4

_JUDGE_PROMPT = """You are scoring an assistant's answer to a task. Judge by the rubric, not by
your taste.

TASK: {instruction}

RUBRIC: {rubric}

ANSWER:
{result}

Reply with exactly one line: "Score: N" where N is 1-5.
5 = fully satisfies the rubric, nothing important missing
3 = partial — some rubric points met, some missed
1 = does not address the task
"""


def run_checks(result: str, checks: list[dict], *, cwd: Path | None = None) -> list[dict]:
    """Run every check, returning one detail dict per check (never raising —
    an invalid check definition is a failed check, not a crash)."""
    details = []
    for i, check in enumerate(checks):
        kind = check.get("kind", "")
        value = check.get("value", "")
        ok = False
        try:
            if kind == "contains":
                ok = value in result
            elif kind == "regex":
                ok = re.search(value, result) is not None
            elif kind == "exit_code":
                proc = subprocess.run(
                    value, shell=True, capture_output=True, text=True, cwd=str(cwd or Path.cwd())
                )
                ok = proc.returncode == 0
        except (re.error, OSError):
            ok = False
        details.append(
            {"index": i, "kind": kind, "value": value, "note": check.get("note", ""), "passed": ok}
        )
    return details


def all_checks_pass(details: list[dict]) -> bool:
    return all(d["passed"] for d in details)


async def llm_judge(router: Any, task: dict, result: str) -> int:
    """The judge role scores the answer against the task rubric, 1–5."""
    prompt = _JUDGE_PROMPT.format(
        instruction=task["instruction"], rubric=task.get("rubric", ""), result=result
    )
    response = await router.judge().ainvoke(prompt)
    match = re.search(r"Score:\s*([1-5])", response.content)
    return int(match.group(1)) if match else 1


async def score(router: Any, task: dict, result: str, *, cwd: Path | None = None) -> dict:
    """Full score of one result: checks + judge. `passed` gates acceptance."""
    details = run_checks(result, task.get("checks", []), cwd=cwd)
    checks_ok = all_checks_pass(details)
    judge = await llm_judge(router, task, result)
    return {
        "checks": details,
        "checks_passed": checks_ok,
        "llm_score": judge,
        "passed": checks_ok and judge >= JUDGE_THRESHOLD,
    }
