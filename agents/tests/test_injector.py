"""Skill library: discovery across both libraries, frontmatter parsing, the
progressive-disclosure index, overflow ranking — and the wiring proof that the
index actually reaches the model as part of the system prompt.

The central behaviour here is that bodies are *not* loaded. The prompt carries
one line per skill and the agent calls read_skill when one applies; pasting
bodies for whatever the keyword match hit spends context on skills that turn
out to be irrelevant and stops scaling past a handful.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage

from daimon_agent.graph import build_graph, close_checkpointer, make_sqlite_checkpointer
from daimon_agent.run import run_turn
from daimon_agent.skills.injector import (
    MAX_INDEXED,
    Skill,
    discover_skills,
    format_skills_block,
    format_skills_index,
    select_skills,
)

from fakes import FakeRouter


def _write_skill(skills_dir: Path, name: str, description: str, body: str) -> None:
    path = skills_dir / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")


# --- discovery ---------------------------------------------------------------

def test_discover_parses_frontmatter_and_body(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "changelog", "Write a changelog section", "## Categories\n\nUse Fixed/Added.")
    _write_skill(skills_dir, "resume", "Tailor a resume", "## Rules\n\nKeep it to one page.")

    skills = discover_skills(skills_dir)
    assert [s.name for s in skills] == ["changelog", "resume"]
    assert skills[0].description == "Write a changelog section"
    assert skills[0].content.startswith("## Categories")
    assert skills[0].source == "vault"


def test_discover_missing_dir_is_empty(tmp_path: Path) -> None:
    assert discover_skills(tmp_path / "nope") == []
    assert discover_skills(None, None) == []


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


def test_discover_merges_both_libraries(tmp_path: Path) -> None:
    vault, project = tmp_path / "vault", tmp_path / "project"
    _write_skill(vault, "changelog", "Global changelog", "vault body")
    _write_skill(project, "deploy", "Ship this repo", "project body")

    skills = discover_skills(vault, project)
    assert [(s.name, s.source) for s in skills] == [
        ("changelog", "vault"),
        ("deploy", "project"),
    ]


def test_project_skill_shadows_the_vault_one(tmp_path: Path) -> None:
    """A repo's own version of a procedure should win over the user's general
    one — the same precedence a project config has over a global one."""
    vault, project = tmp_path / "vault", tmp_path / "project"
    _write_skill(vault, "deploy", "Generic deploy", "generic body")
    _write_skill(project, "deploy", "Deploy THIS repo", "specific body")

    skills = discover_skills(vault, project)
    assert len(skills) == 1
    assert skills[0].source == "project"
    assert skills[0].content == "specific body"


# --- selection ---------------------------------------------------------------

def test_everything_is_listed_under_the_cap() -> None:
    """A one-line index is cheap, and the agent judges relevance better than
    token overlap does — so nothing is filtered out on a small library."""
    skills = [
        Skill(name="changelog", description="Write a changelog"),
        Skill(name="bread", description="Bake sourdough"),
    ]
    assert select_skills("something unrelated entirely", skills) == skills


def test_overflow_falls_back_to_ranking() -> None:
    """Past the cap the index would cost more than it saves, so overlap picks
    which skills make it in."""
    skills = [Skill(name=f"skill-{i}", description=f"topic {i}") for i in range(MAX_INDEXED + 10)]
    skills.append(Skill(name="changelog", description="Write a changelog section"))

    picked = select_skills("write a changelog for the release", skills)
    assert len(picked) == MAX_INDEXED
    assert picked[0].name == "changelog"  # best overlap first


# --- formatting --------------------------------------------------------------

def test_index_lists_names_and_descriptions_without_bodies() -> None:
    skill = Skill(name="changelog", description="Write a changelog", content="SECRET BODY")
    index = format_skills_index([skill])
    assert "# Available skills" in index
    assert "- changelog: Write a changelog" in index
    assert "read_skill" in index  # tells the agent how to get the body
    assert "SECRET BODY" not in index


def test_index_marks_project_skills() -> None:
    index = format_skills_index([Skill(name="deploy", description="Ship it", source="project")])
    assert "- deploy (project): Ship it" in index


def test_index_handles_a_missing_description() -> None:
    assert "(no description)" in format_skills_index([Skill(name="x", description="")])


def test_index_empty() -> None:
    assert format_skills_index([]) == ""


def test_format_skills_block_still_inlines_bodies() -> None:
    """The optimizer's rollouts vary a skill's *body* between candidate and
    baseline, so the body has to reach the model for the score delta to mean
    anything. Removing this would break validation silently."""
    skill = Skill(name="changelog", description="Write a changelog", content="## Categories\n\nUse Fixed.")
    block = format_skills_block([skill])
    assert block.startswith("# Reusable skills")
    assert "## Skill: changelog — Write a changelog" in block
    assert "Use Fixed." in block


def test_format_skills_block_empty() -> None:
    assert format_skills_block([]) == ""


# --- wiring ------------------------------------------------------------------

async def test_run_turn_injects_the_index_not_the_body(settings, tmp_path) -> None:
    """The wiring: the library reaches the model as an index after the rules
    text, and the body stays out until read_skill asks for it."""
    checkpointer = await make_sqlite_checkpointer(tmp_path / "cp.db")
    router = FakeRouter(pro_script=[AIMessage(content="done")])
    graph = build_graph(settings, router, [], checkpointer=checkpointer)
    skills = [
        Skill(
            name="changelog",
            description="Write a changelog section",
            content="THE BODY: use Fixed/Added.",
        )
    ]

    try:
        await run_turn(
            "write a changelog for the release",
            "inj",
            settings,
            [].append,
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
    assert "# Available skills" in system_text
    assert "- changelog: Write a changelog section" in system_text
    assert "THE BODY" not in system_text, "the body should load only via read_skill"
    assert system_text.index("You are Daimon") < system_text.index("# Available skills")
