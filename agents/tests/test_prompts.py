"""The system prompt's two halves: a byte-stable rules prefix that the
providers' prefix caches depend on, and a per-turn tail carrying everything
that changes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from daimon_agent.prompts import (
    DAIMON_RULES,
    MAX_PROJECT_CONTEXT_CHARS,
    build_system_prompt,
    environment_block,
    project_context,
)


def test_environment_block_names_where_the_agent_actually_is(settings, tmp_path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    block = environment_block(replace(settings, workspace_dir=ws))

    assert str(ws) in block
    assert str(settings.vault_dir) in block
    assert "Platform:" in block


def test_environment_block_reports_the_git_branch(settings, tmp_path) -> None:
    import subprocess

    ws = tmp_path / "repo"
    ws.mkdir()
    if subprocess.run(["git", "init", "-q"], cwd=ws).returncode != 0:
        return  # no git on this machine; the block degrades to silence
    (ws / "a.txt").write_text("x")

    block = environment_block(replace(settings, workspace_dir=ws))

    assert "Git branch:" in block
    assert "uncommitted changes" in block


def test_no_git_is_silence_not_an_error(settings, tmp_path) -> None:
    ws = tmp_path / "plain"
    ws.mkdir()
    assert "Git branch" not in environment_block(replace(settings, workspace_dir=ws))


def test_project_context_reads_the_workspace_file(settings, tmp_path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "DAIMON.md").write_text("Run the tests with `just test`.")

    out = project_context(replace(settings, workspace_dir=ws))

    assert "just test" in out
    assert "DAIMON.md" in out


def test_daimon_md_wins_over_the_others(settings, tmp_path) -> None:
    """Two of them would only contradict each other. Most specific wins."""
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "DAIMON.md").write_text("the daimon one")
    (ws / "AGENTS.md").write_text("the agents one")
    (ws / "CLAUDE.md").write_text("the claude one")

    out = project_context(replace(settings, workspace_dir=ws))

    assert "the daimon one" in out
    assert "the agents one" not in out


def test_agents_md_is_read_when_there_is_no_daimon_md(settings, tmp_path) -> None:
    """A repo that already wrote down what an agent needs has already done the
    work; asking the user to duplicate it under a third name is a poor trade."""
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("source lives in src/")

    assert "source lives in src/" in project_context(replace(settings, workspace_dir=ws))


def test_global_and_project_context_both_land(settings, tmp_path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "DAIMON.md").write_text("project rule")
    Path(settings.resolved_global_context).write_text("global rule")

    out = project_context(replace(settings, workspace_dir=ws))

    # Global first, so the project section reads as the more specific override.
    assert out.index("global rule") < out.index("project rule")


def test_project_context_is_capped(settings, tmp_path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "DAIMON.md").write_text("x" * (MAX_PROJECT_CONTEXT_CHARS * 2))

    out = project_context(replace(settings, workspace_dir=ws))

    assert "truncated at" in out
    assert len(out) < MAX_PROJECT_CONTEXT_CHARS * 1.5


def test_no_context_files_is_an_empty_block(settings, tmp_path) -> None:
    ws = tmp_path / "bare"
    ws.mkdir()
    assert project_context(replace(settings, workspace_dir=ws)) == ""


def test_the_rules_prefix_stays_byte_identical(settings, tmp_path) -> None:
    """The whole point of the prefix/tail split: DeepSeek caches the prefix
    automatically and Anthropic is told to. A prefix that moves never caches."""
    ws = tmp_path / "project"
    ws.mkdir()
    bare = build_system_prompt(replace(settings, workspace_dir=ws), date_line_text="DATE")

    (ws / "DAIMON.md").write_text("standing instructions appeared")
    with_context = build_system_prompt(
        replace(settings, workspace_dir=ws),
        skills_block="# Available skills\n- a: b",
        date_line_text="DATE",
    )

    assert bare.startswith(DAIMON_RULES)
    assert with_context.startswith(DAIMON_RULES)
    assert "standing instructions appeared" in with_context
    # Everything that varies sits after the frozen prefix, never inside it.
    assert "standing instructions appeared" not in DAIMON_RULES
