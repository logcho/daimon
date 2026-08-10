"""Tools whose bodies had no coverage — the ones where a plain NameError sat
undetected because a tool failure surfaces as a message, not a crash."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from daimon_agent.tools import build_tools


@pytest.fixture
def tools(settings, tmp_path):
    return {
        t.name: t
        for t in build_tools(
            replace(settings, vault_dir=tmp_path, workspace_dir=None),
            memory=None,
            session_id="tools-test",
        )
    }


async def test_debug_runs_code_and_returns_output(tools) -> None:
    """Regression: this raised `NameError: _SECRET_ENV` on every call, and the
    graph swallowed it into a tool-error message so nothing ever noticed."""
    out = await tools["debug"].ainvoke({"code": "print(6 * 7)"})
    assert "42" in out
    assert "NameError" not in out


async def test_debug_scrubs_secrets_from_the_subprocess(tools, monkeypatch) -> None:
    """The reason that variable was referenced at all."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-leak")
    out = await tools["debug"].ainvoke({
        "code": "import os; print('LEAKED' if os.environ.get('DEEPSEEK_API_KEY') else 'clean')"
    })
    assert "clean" in out
    assert "LEAKED" not in out


async def test_debug_reports_a_traceback_on_failure(tools) -> None:
    out = await tools["debug"].ainvoke({"code": "1 / 0"})
    assert "ZeroDivisionError" in out


async def test_mkdir_and_list_directory_round_trip(tools) -> None:
    await tools["mkdir"].ainvoke({"path": "sub/dir"})
    out = await tools["list_directory"].ainvoke({"path": "sub"})
    assert "dir" in out


async def test_delete_and_move_file(tools, tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    out = await tools["move_file"].ainvoke({"source": "a.txt", "destination": "b.txt"})
    assert "b.txt" in out
    assert (tmp_path / "b.txt").exists()
    await tools["delete_file"].ainvoke({"file_path": "b.txt"})
    assert not (tmp_path / "b.txt").exists()


async def test_update_todos_is_wired_to_the_session(tools) -> None:
    from daimon_agent.tools.todo import get_todos

    out = await tools["update_todos"].ainvoke({"todos": "in_progress|do the thing"})
    assert "0/1 done" in out
    assert get_todos("tools-test")[0]["text"] == "do the thing"


# --- skills: bundled files ---------------------------------------------------

def _install(root, name: str, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / name / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


async def test_read_skill_lists_what_else_is_bundled(settings, tmp_path) -> None:
    """An installed skill is a directory. Its SKILL.md says "see REFERENCE.md",
    and those siblings live in the vault — outside the workspace, so the
    workspace-confined file tools can't reach them. Reading the skill has to
    say what's there and how to get it."""
    from dataclasses import replace

    from daimon_agent.tools import skills_tools

    cfg = replace(settings, vault_dir=tmp_path, workspace_dir=None)
    _install(cfg.resolved_skills_dir, "pdf", {
        "SKILL.md": "---\nname: pdf\ndescription: PDFs\n---\n\nSee reference.md.",
        "reference.md": "# Reference\n\nThe details.",
        "scripts/fill.py": "print('x')",
    })

    body = skills_tools.read_skill(cfg, "pdf")
    assert "See reference.md" in body
    assert "reference.md" in body and "scripts/fill.py" in body
    assert str(cfg.resolved_skills_dir / "pdf") in body  # runnable path for scripts


async def test_read_skill_reads_a_bundled_file(settings, tmp_path) -> None:
    from dataclasses import replace

    from daimon_agent.tools import skills_tools

    cfg = replace(settings, vault_dir=tmp_path, workspace_dir=None)
    _install(cfg.resolved_skills_dir, "pdf", {
        "SKILL.md": "---\nname: pdf\n---\n\nbody",
        "reference.md": "# The details",
    })
    assert "The details" in skills_tools.read_skill(cfg, "pdf", "reference.md")


async def test_read_skill_will_not_escape_the_skill_directory(settings, tmp_path) -> None:
    """A skill's text can come from a stranger's repo, so a path it suggests
    must not become a read of anything it likes."""
    from dataclasses import replace

    from daimon_agent.tools import skills_tools

    cfg = replace(settings, vault_dir=tmp_path, workspace_dir=None)
    _install(cfg.resolved_skills_dir, "pdf", {"SKILL.md": "---\nname: pdf\n---\n\nbody"})
    (tmp_path / "secret.txt").write_text("private")

    out = skills_tools.read_skill(cfg, "pdf", "../../secret.txt")
    assert "outside the skill directory" in out
    assert "private" not in out


async def test_read_skill_missing_file_is_a_message(settings, tmp_path) -> None:
    from dataclasses import replace

    from daimon_agent.tools import skills_tools

    cfg = replace(settings, vault_dir=tmp_path, workspace_dir=None)
    _install(cfg.resolved_skills_dir, "pdf", {"SKILL.md": "---\nname: pdf\n---\n\nbody"})
    assert "not a file" in skills_tools.read_skill(cfg, "pdf", "nope.md")


async def test_save_skill_scope_picks_the_library(settings, tmp_path) -> None:
    from dataclasses import replace

    from daimon_agent.tools import skills_tools

    cfg = replace(settings, vault_dir=tmp_path / "vault", workspace_dir=tmp_path / "ws")
    skills_tools.save_skill(cfg, None, "global-one", "d", "body")
    skills_tools.save_skill(cfg, None, "repo-one", "d", "body", scope="project")

    assert (cfg.resolved_skills_dir / "global-one" / "SKILL.md").is_file()
    assert (cfg.project_skills_dir / "repo-one" / "SKILL.md").is_file()
