"""The SkillOpt-style optimizer — text-space evolution with a validation gate.

propose_edit: the Pro model proposes bounded edits (<= 3 add/delete/replace
blocks, each anchored to a "## " section heading) to a skill's markdown body,
from training rollouts scored by scorer.score.

validate: the strict gate — the edited skill must beat the baseline on the
held-out split (strict improvements > regressions; ties never count as wins).
Only a passing candidate is written to best_skill.md.

This is deliberately the basic optimizer, not SkillOpt's self-play engine:
one proposal per round, section-level edits, deterministic parsing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .scorer import score

MAX_EDIT_BLOCKS = 3

_PROPOSE_PROMPT = """You are improving a skill for an agent. The skill body is markdown with "## "
section headings. You have training-task results: the current skill's scores
(1-5 each) and its answers.

SKILL NAME: {name}
CURRENT BODY:
{body}

TRAINING RESULTS (task -> score, answer excerpt):
{training}

Propose at most {max_blocks} edits. Each edit is one JSON object:
  {{"op": "add" | "delete" | "replace", "heading": "<section heading>", "text": "<new body>"}}
- add: append a new section with the given heading and text
- delete: remove the section with that heading (text may be empty)
- replace: rewrite the body of the section with that heading (keep the heading)

Rules: never more than {max_blocks} edits; every heading must be a short
kebab-case or title-case phrase; a delete or replace must target a heading
that exists in the current body. Fix what the training results show is
broken — do not churn sections that are already working.

Reply with ONLY a JSON array of edits, no prose.
"""

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


def _extract_ops(response: str) -> list[dict]:
    """Lenient parse: the model sometimes wraps the array in a fenced code
    block or adds a trailing period. Any unparseable op is dropped."""
    match = _JSON_ARRAY.search(response)
    if not match:
        return []
    try:
        ops = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(ops, list):
        return []
    cleaned = []
    for op in ops[:MAX_EDIT_BLOCKS]:
        if isinstance(op, dict) and op.get("op") in ("add", "delete", "replace"):
            cleaned.append(
                {
                    "op": op["op"],
                    "heading": str(op.get("heading", "")).strip(),
                    "text": str(op.get("text", "")).strip(),
                }
            )
    return cleaned


def _split_sections(content: str) -> list[tuple[str, str]]:
    """[(heading_without_markers, body)] — sections at "## " headings."""
    parts = re.split(r"\n(?=## )", f"\n{content}")
    sections = []
    for part in parts:
        part = part.strip("\n")
        if not part:
            continue
        if part.startswith("## "):
            line, _, rest = part.partition("\n")
            sections.append((line[3:].strip(), rest.strip()))
        else:  # preamble before the first heading
            sections.append(("", part))
    return sections


def apply_ops(content: str, ops: list[dict]) -> str:
    """Apply parsed ops to the body. add always appends; delete/replace match
    headings case-insensitively and are no-ops when the heading is absent."""
    sections = _split_sections(content)
    for op in ops:
        heading = op["heading"].lower()
        if op["op"] == "add":
            sections.append((op["heading"], op["text"]))
            continue
        for i, (h, _) in enumerate(sections):
            if h.lower() == heading:
                if op["op"] == "delete":
                    del sections[i]
                else:  # replace: keep the heading, swap the body
                    sections[i] = (h, op["text"])
                break
    return "\n\n".join(f"## {h}\n\n{b}" if h else b for h, b in sections).strip() + "\n"


async def propose_edit(router: Any, skill: Any, training: list[dict]) -> str | None:
    """Ask the Pro model for bounded edits; returns the edited body, or None
    when the response yields no usable ops."""
    rows = "\n".join(
        f"- {t['task']['instruction']}: score {t['score']['llm_score']} "
        f"(checks: {t['score']['checks_passed']})\n  {t['result'][:400]}"
        for t in training
    )
    prompt = _PROPOSE_PROMPT.format(
        name=skill.name, body=skill.content, training=rows, max_blocks=MAX_EDIT_BLOCKS
    )
    response = await router.pro().ainvoke(prompt)
    ops = _extract_ops(response.content)
    if not ops:
        return None
    return apply_ops(skill.content, ops)


async def validate(
    settings: Any,
    router: Any,
    tools: list[Any],
    candidate: str,
    baseline: str,
    held_out: list[dict],
) -> dict:
    """The strict gate: score the candidate and the baseline on every
    held-out task (full rollout each — only the skill tail varies). Acceptance
    requires strictly more wins than regressions; ties never count."""
    from .rollout import rollout

    stats = {"wins": 0, "losses": 0, "ties": 0, "details": []}
    for task in held_out:
        cand_result = await rollout(settings, router, tools, task, candidate)
        base_result = await rollout(settings, router, tools, task, baseline)
        cand_score = await score(router, task, cand_result)
        base_score = await score(router, task, base_result)

        if cand_score["passed"] and not base_score["passed"]:
            stats["wins"] += 1
        elif base_score["passed"] and not cand_score["passed"]:
            stats["losses"] += 1
        elif cand_score["llm_score"] > base_score["llm_score"]:
            stats["wins"] += 1
        elif cand_score["llm_score"] < base_score["llm_score"]:
            stats["losses"] += 1
        else:
            stats["ties"] += 1
        stats["details"].append(
            {
                "instruction": task["instruction"],
                "candidate": {"score": cand_score["llm_score"], "passed": cand_score["passed"]},
                "baseline": {"score": base_score["llm_score"], "passed": base_score["passed"]},
            }
        )
    stats["accepted"] = stats["wins"] > stats["losses"]
    return stats


def write_best_skill(settings: Any, skill: Any, content: str) -> Path:
    """best_skill.md: the gated winner, written alongside the working SKILL.md
    so the user can diff and adopt it."""
    target = Path(settings.resolved_skills_dir) / skill.name / "best_skill.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n\n{content}"
    target.write_text(text, encoding="utf-8")
    return target
