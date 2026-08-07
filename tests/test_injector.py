"""Skill library: discovery, frontmatter parsing, ranking, prompt-tail
formatting — and the wiring proof that a ranked skill actually reaches the
model as part of the system prompt."""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage

from daimon_agent.graph import build_graph, close_checkpointer, make_sqlite_checkpointer
from daimon_agent.run import run_turn
from daimon_agent.skills.injector import (
    Skill,
    discover_skills,
    format_skills_block,
    select_skills,
)

from fakes import FakeRouter


def _write_skill(skills_dir: Path, name: str, description: str, body: str) -> None:
    path = skills_dir / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")


def test_discover_parses_frontmatter_and_body(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "changelog", "Write a changelog section", "## Categories\n\nUse Fixed/Added.")
    _write_skill(skills_dir, "resume", "Tailor a resume", "## Rules\n\nKeep it to one page.")

    skills = discover_skills(skills_dir)
    assert [s.name for s in skills] == ["changelog", "resume"]
    assert skills[0].description == "Write a changelog section"
    assert skills[0].content.startswith("## Categories")


def test_discover_missing_dir_is_empty(tmp_path: Path) -> None:
    assert discover_skills(tmp_path / "nope") == []


def test_discover_degrades_malformed_frontmatter(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    (skills_dir / "broken").mkdir(parents=True)
    (skills_dir / "broken" / "SKILL.md").write_text("no frontmatter here\n\njust body\n")

    skills = discover_skills(skills_dir)
    assert len(skills) == 1
    assert skills[0].name == "broken"  # falls back to the directory name
    assert skills[0].content == "no frontmatter here\n\njust body"


def test_discover_skips_unreadable_skill(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "good", "Fine skill", "body")
    (skills_dir / "good" / "SKILL.md").unlink()
    (skills_dir / "good" / "SKILL.md").mkdir()  # a directory where the file should be

    assert discover_skills(skills_dir) == []


def test_select_ranks_by_overlap_and_caps_at_k() -> None:
    changelog = Skill(name="changelog", description="Write a changelog section")
    resume = Skill(name="resume", description="Tailor a resume to a posting")
    notes = Skill(name="notes", description="Capture meeting notes")

    picked = select_skills("write a changelog for the release", [resume, notes, changelog], k=1)
    assert picked == [changelog]  # best overlap first, capped at k


def test_select_ignores_skills_with_no_overlap() -> None:
    picked = select_skills("bake bread", [Skill(name="changelog", description="Write a changelog")])
    assert picked == []


def test_format_skills_block_structure() -> None:
    skill = Skill(name="changelog", description="Write a changelog", content="## Categories\n\nUse Fixed.")
    block = format_skills_block([skill])
    assert block.startswith("# Reusable skills")
    assert "## Skill: changelog — Write a changelog" in block
    assert "Use Fixed." in block


def test_format_skills_block_empty() -> None:
    assert format_skills_block([]) == ""


async def test_run_turn_injects_ranked_skill_into_system_prompt(settings, tmp_path) -> None:
    """The wiring: a matching skill appears in the model's input as part of
    the frozen-prefix system prompt, after the rules text."""
    checkpointer = await make_sqlite_checkpointer(tmp_path / "cp.db")
    router = FakeRouter(pro_script=[AIMessage(content="done")])
    graph = build_graph(settings, router, [], checkpointer=checkpointer)
    skills = [Skill(name="changelog", description="Write a changelog section", content="Use Fixed/Added.")]
    events: list[dict] = []

    try:
        await run_turn(
            "write a changelog for the release",
            "inj",
            settings,
            events.append,
            graph=graph,
            skills=skills,
        )
    finally:
        await close_checkpointer(checkpointer)

    system_text = ""
    for message in router._pro.calls[0]:
        if isinstance(message, SystemMessage):
            system_text = str(message.content)
    assert "You are Daimon" in system_text  # frozen prefix still first
    assert "# Reusable skills" in system_text  # ranked tail appended
    assert "Use Fixed/Added." in system_text
    assert system_text.index("You are Daimon") < system_text.index("# Reusable skills")
