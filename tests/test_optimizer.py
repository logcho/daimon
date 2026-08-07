"""The optimizer core: bounded-edit parsing and application, propose_edit with
a scripted Pro model, and the strict held-out validation gate."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from daimon_agent.graph import build_graph
from daimon_agent.optimizer.optimizer import (
    MAX_EDIT_BLOCKS,
    _extract_ops,
    apply_ops,
    propose_edit,
    validate,
)
from daimon_agent.skills.injector import Skill

from fakes import FakeRouter

BODY = "## Approach\n\nDo it carefully.\n\n## Pitfalls\n\nDon't rush."


# -- edit parsing and application ------------------------------------------

def test_extract_ops_parses_fenced_json_and_truncates() -> None:
    response = (
        'Here are the edits:\n```json\n'
        '[{"op": "replace", "heading": "Approach", "text": "Do it fast."},\n'
        ' {"op": "add", "heading": "Examples", "text": "Show one."},\n'
        ' {"op": "delete", "heading": "Pitfalls", "text": ""},\n'
        ' {"op": "add", "heading": "Fourth", "text": "ignored"}]\n```'
    )
    ops = _extract_ops(response)
    assert len(ops) == MAX_EDIT_BLOCKS  # bounded
    assert ops[0] == {"op": "replace", "heading": "Approach", "text": "Do it fast."}
    assert ops[-1]["op"] == "delete"


def test_extract_ops_rejects_garbage() -> None:
    assert _extract_ops("I don't think the skill needs changes.") == []
    assert _extract_ops('{"op": "add", "heading": "x"}') == []  # not a list
    assert _extract_ops('[{"op": "rewrite", "heading": "x", "text": "y"}]') == []  # bad op


def test_apply_ops_replace_keeps_heading_and_body_swaps() -> None:
    out = apply_ops(BODY, [{"op": "replace", "heading": "Approach", "text": "Do it fast."}])
    assert "## Approach\n\nDo it fast." in out
    assert "Do it carefully." not in out
    assert "Pitfalls" in out  # untouched sections survive


def test_apply_ops_delete_and_add() -> None:
    out = apply_ops(BODY, [{"op": "delete", "heading": "Pitfalls", "text": ""}])
    assert "Pitfalls" not in out
    out = apply_ops(out, [{"op": "add", "heading": "Examples", "text": "Show one."}])
    assert "## Examples\n\nShow one." in out


def test_apply_ops_heading_match_is_case_insensitive_and_noop_when_absent() -> None:
    out = apply_ops(BODY, [{"op": "delete", "heading": "pitfalls", "text": ""}])
    assert "Pitfalls" not in out
    out = apply_ops(BODY, [{"op": "replace", "heading": "Nonexistent", "text": "x"}])
    assert out.rstrip() == BODY  # no-op, body unchanged (trailing newline normalized)


def test_apply_ops_preserves_preamble() -> None:
    body = "Intro paragraph.\n\n## Approach\n\nDo it.\n"
    out = apply_ops(body, [{"op": "replace", "heading": "Approach", "text": "Done."}])
    assert out.startswith("Intro paragraph.")


# -- propose_edit -----------------------------------------------------------

async def test_propose_edit_returns_edited_body() -> None:
    router = FakeRouter(pro_script=[AIMessage(content='[{"op": "replace", "heading": "Approach", "text": "Do it fast."}]')])
    skill = Skill(name="tune", description="Tune things", content=BODY)

    out = await propose_edit(router, skill, [])

    assert out is not None
    assert "Do it fast." in out
    assert "Do it carefully." not in out
    # The proposal prompt carried the skill body and bounded-edit rules.
    prompt = str(router._pro.calls[0][0].content)
    assert "Do it carefully." in prompt
    assert str(MAX_EDIT_BLOCKS) in prompt


async def test_propose_edit_returns_none_on_no_usable_ops() -> None:
    router = FakeRouter(pro_script=[AIMessage(content="No edits needed.")])
    skill = Skill(name="tune", description="Tune things", content=BODY)
    assert await propose_edit(router, skill, []) is None


# -- validation gate --------------------------------------------------------

def _held_out_tasks(count: int) -> list[dict]:
    return [{"instruction": f"task {i}", "rubric": "Be correct.", "checks": []} for i in range(count)]


async def test_validate_accepts_strict_improvement(settings) -> None:
    """Task 1: candidate 5 vs baseline 2 (win). Task 2: 4 vs 4 (tie — never a
    win). Wins (1) > losses (0) -> accepted. Pro is scripted per rollout; the
    judge script interleaves candidate/baseline scores per task."""
    pro_script = [
        AIMessage(content="candidate answer one"),
        AIMessage(content="baseline answer one"),
        AIMessage(content="candidate answer two"),
        AIMessage(content="baseline answer two"),
    ]
    judge_script = [
        AIMessage(content="Score: 5"),  # task 1 candidate
        AIMessage(content="Score: 2"),  # task 1 baseline
        AIMessage(content="Score: 4"),  # task 2 candidate
        AIMessage(content="Score: 4"),  # task 2 baseline (tie)
    ]
    router = FakeRouter(pro_script=pro_script, judge_script=judge_script)

    verdict = await validate(settings, router, [], "candidate body", "baseline body", _held_out_tasks(2))

    assert verdict["wins"] == 1 and verdict["losses"] == 0 and verdict["ties"] == 1
    assert verdict["accepted"] is True


async def test_validate_rejects_regression(settings) -> None:
    pro_script = [
        AIMessage(content="candidate answer"),
        AIMessage(content="baseline answer"),
    ]
    judge_script = [
        AIMessage(content="Score: 2"),  # candidate worse
        AIMessage(content="Score: 5"),  # baseline better
    ]
    router = FakeRouter(pro_script=pro_script, judge_script=judge_script)

    verdict = await validate(settings, router, [], "candidate body", "baseline body", _held_out_tasks(1))

    assert verdict["wins"] == 0 and verdict["losses"] == 1
    assert verdict["accepted"] is False


async def test_validate_rollouts_only_vary_the_skill_tail(settings) -> None:
    """The two rollouts for one task hit the same graph; their only input
    difference must be the skills_block. Verified through the pro model's
    recorded system prompt."""
    router = FakeRouter(
        pro_script=[AIMessage(content="answer"), AIMessage(content="answer")],
        judge_script=[AIMessage(content="Score: 3"), AIMessage(content="Score: 3")],
    )

    await validate(settings, router, [], "candidate body", "baseline body", _held_out_tasks(1))

    prompts = [str(call[0].content) for call in router._pro.calls]
    assert len(prompts) == 2
    # Same rules prefix (up to the timestamped date line, which may tick
    # between rollouts), different skill tails — nothing else varies.
    def prefix(p: str) -> str:
        return p.split("Current date/time")[0]
    assert prefix(prompts[0]) == prefix(prompts[1])
    assert "candidate body" in prompts[0] and "baseline body" not in prompts[0]
    assert "baseline body" in prompts[1] and "candidate body" not in prompts[1]
