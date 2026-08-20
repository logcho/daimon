"""SkillOpt-style offline optimizer: evolve a skill against a task corpus.

Usage:
    uv run python scripts/optimize_skill.py \
        --skill example \
        --corpus corpus/example/tasks.json \
        [--rounds 1] [--split 0.7]

Each round: roll out the current skill on the training split, have the Pro
model propose bounded section edits, then validate the candidate against the
baseline on the held-out split — accepted only on strict improvement (wins >
losses; ties never count). A gated best_skill.md is written next to the
working SKILL.md.

Requires a real DEEPSEEK_API_KEY (rollouts hit the Pro model headlessly; the
judge runs as its own role at temperature 0.7).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from daimon_agent.config import Settings
from daimon_agent.model import ModelRouter
from daimon_agent.optimizer.optimizer import propose_edit, validate, write_best_skill
from daimon_agent.optimizer.scorer import score
from daimon_agent.optimizer.rollout import rollout
from daimon_agent.skills.injector import discover_skills
from daimon_agent.tools import build_tools


def _load_corpus(path: Path) -> list[dict]:
    tasks = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(tasks, list) or not tasks:
        raise SystemExit(f"corpus {path} must be a non-empty list of tasks")
    for i, task in enumerate(tasks):
        if "instruction" not in task:
            raise SystemExit(f"corpus task {i} is missing 'instruction'")
    return tasks


def _bootstrap_skill(skill_name: str, skills_dir: Path) -> None:
    """Fresh checkouts have no vault yet (vault/ is gitignored). If the named
    skill is missing, copy corpus/<skill>/SKILL.md next to the corpus — the
    optimizer needs a baseline to evolve from."""
    source = Path("corpus") / skill_name / "SKILL.md"
    if not source.exists():
        return
    target = skills_dir / skill_name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"bootstrapped starter skill from {source} -> {target}", file=sys.stderr)


async def _amain(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    router = ModelRouter(settings)
    tools = build_tools(settings)  # full registry — skills may use any tool

    _bootstrap_skill(args.skill, settings.resolved_skills_dir)
    skills = discover_skills(settings.resolved_skills_dir)
    skill = next((s for s in skills if s.name == args.skill), None)
    if skill is None:
        print(f"skill '{args.skill}' not found in {settings.resolved_skills_dir}", file=sys.stderr)
        return 1

    tasks = _load_corpus(Path(args.corpus))
    split_at = max(1, int(len(tasks) * args.split))
    train, held_out = tasks[:split_at], tasks[split_at:]
    print(f"skill: {skill.name} | corpus: {len(tasks)} tasks "
          f"({len(train)} train / {len(held_out)} held-out) | rounds: {args.rounds}", file=sys.stderr)

    baseline = skill.content
    current = baseline
    for round_no in range(1, args.rounds + 1):
        print(f"\n--- round {round_no}/{args.rounds} ---", file=sys.stderr)

        # Train: roll out the current skill, score each result.
        training = []
        for task in train:
            result = await rollout(settings, router, tools, task, current)
            result_score = await score(router, task, result)
            training.append({"task": task, "result": result, "score": result_score})
            print(f"  train [{result_score['llm_score']}/5 checks={result_score['checks_passed']}] "
                  f"{task['instruction'][:60]}", file=sys.stderr)

        candidate = await propose_edit(router, skill, training)
        if candidate is None or candidate == current:
            print("  propose: no usable edits (skipping round)", file=sys.stderr)
            continue

        verdict = await validate(settings, router, tools, candidate, baseline, held_out)
        print(f"  validate: wins={verdict['wins']} losses={verdict['losses']} "
              f"ties={verdict['ties']} -> {'ACCEPT' if verdict['accepted'] else 'REJECT'}",
              file=sys.stderr)
        for detail in verdict["details"]:
            print(f"    {detail['instruction'][:50]}: candidate {detail['candidate']['score']}/5 "
                  f"vs baseline {detail['baseline']['score']}/5", file=sys.stderr)
        if verdict["accepted"]:
            current = candidate

    if current != baseline:
        path = write_best_skill(settings, skill, current)
        print(f"\naccepted after {args.rounds} round(s) — wrote {path}", file=sys.stderr)
    else:
        print("\nno accepted improvement — best_skill.md not written", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", default="example")
    parser.add_argument("--corpus", default="corpus/example/tasks.json")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--split", type=float, default=0.7)
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
