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


async def test_agent_cannot_choose_project_scope(settings, tmp_path) -> None:
    """The library above still supports both scopes, but the *agent* doesn't
    get to pick. It used to, and skills it filed under 'project' vanished from
    every session whose workspace wasn't that project — including the app,
    which is where the user looks at them. Structural rather than a prompt
    instruction, because the model ignored the prompt."""
    cfg = replace(settings, vault_dir=tmp_path / "vault", workspace_dir=tmp_path / "ws")
    tool = {
        t.name: t for t in build_tools(cfg, memory=None, session_id="scope-test")
    }["save_skill"]

    assert "scope" not in tool.args_schema.model_fields

    await tool.ainvoke({"name": "written-by-agent", "description": "d", "content": "body"})
    assert (cfg.resolved_skills_dir / "written-by-agent" / "SKILL.md").is_file()
    assert not (cfg.project_skills_dir / "written-by-agent").exists()

    # And a model that passes it anyway — they do invent arguments — has it
    # ignored, not honoured: the skill still lands in the vault library.
    out = await tool.ainvoke({
        "name": "sneaky", "description": "d", "content": "body", "scope": "project",
    })
    assert (cfg.resolved_skills_dir / "sneaky" / "SKILL.md").is_file(), out
    assert not (cfg.project_skills_dir / "sneaky").exists(), out


# --- search that stays inside the project ------------------------------------

def test_grep_prunes_ignored_directories(settings, tmp_path) -> None:
    """A workspace with a .venv used to be walked in full: rglob cannot prune,
    so every vendored file was opened and decoded before being filtered out."""
    from daimon_agent.tools.files import grep_files
    from daimon_agent.workspace import Confinement

    root = tmp_path / "ws"
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / "node_modules").mkdir()
    (root / "src").mkdir()
    (root / ".venv" / "lib" / "vendored.py").write_text("NEEDLE in a vendored file\n")
    (root / "node_modules" / "dep.js").write_text("NEEDLE in a dependency\n")
    (root / "src" / "mine.py").write_text("NEEDLE in my own code\n")

    out = grep_files(Confinement(root), "NEEDLE", "")

    assert "src/mine.py" in out
    assert ".venv" not in out
    assert "node_modules" not in out


def test_grep_caps_total_matches_not_matches_per_file(settings, tmp_path) -> None:
    """The cap used to sit in the outer per-file loop, so one file with
    thousands of hits returned every one of them."""
    from daimon_agent.tools.files import MAX_RESULTS, grep_files
    from daimon_agent.workspace import Confinement

    root = tmp_path / "ws"
    root.mkdir()
    (root / "big.txt").write_text("\n".join(f"NEEDLE {i}" for i in range(500)))

    out = grep_files(Confinement(root), "NEEDLE", "")
    body, _, note = out.partition("\n…(")

    assert len(body.splitlines()) == MAX_RESULTS
    assert "more remain" in note


def test_grep_can_search_a_single_file(settings, tmp_path) -> None:
    """file_path naming a file used to be walked as a directory, which yields
    nothing — so the narrowing move the truncation note recommends silently
    returned 'no matches'."""
    from daimon_agent.tools.files import grep_files
    from daimon_agent.workspace import Confinement

    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("NEEDLE here\n")
    (root / "b.py").write_text("NEEDLE there\n")

    out = grep_files(Confinement(root), "NEEDLE", "a.py")

    assert "a.py:1" in out
    assert "b.py" not in out


def test_grep_skips_binary_files(settings, tmp_path) -> None:
    from daimon_agent.tools.files import grep_files
    from daimon_agent.workspace import Confinement

    root = tmp_path / "ws"
    root.mkdir()
    (root / "image.bin").write_bytes(b"\x00\x01NEEDLE\x00")
    (root / "text.txt").write_text("NEEDLE\n")

    out = grep_files(Confinement(root), "NEEDLE", "")

    assert "text.txt" in out
    assert "image.bin" not in out


def test_glob_prunes_ignored_directories(settings, tmp_path) -> None:
    from daimon_agent.tools.files import glob_files
    from daimon_agent.workspace import Confinement

    root = tmp_path / "ws"
    (root / ".venv").mkdir(parents=True)
    (root / "src").mkdir()
    (root / ".venv" / "vendored.py").write_text("x")
    (root / "src" / "mine.py").write_text("x")

    out = glob_files(Confinement(root), "*.py")

    assert out == "src/mine.py"


# --- read before you write ----------------------------------------------------

@pytest.fixture
def ws(tmp_path):
    """A confined workspace plus a clean read registry."""
    from daimon_agent.tools.files import forget_reads
    from daimon_agent.workspace import Confinement

    root = tmp_path / "ws"
    root.mkdir()
    forget_reads()
    yield Confinement(root)
    forget_reads()


def test_editing_an_unread_file_is_refused(ws) -> None:
    """The old_text is a guess until the agent has actually looked. The prompt
    always said 'read first'; nothing enforced it."""
    from daimon_agent.tools.files import edit_file

    (ws.root / "a.py").write_text("x = 1\n")

    out = edit_file(ws, "a.py", "x = 1", "x = 2", "s1")

    assert "have not read it" in out
    assert (ws.root / "a.py").read_text() == "x = 1\n"  # untouched


def test_read_then_edit_goes_through(ws) -> None:
    from daimon_agent.tools.files import edit_file, read_file

    (ws.root / "a.py").write_text("x = 1\n")
    read_file(ws, "a.py", session_id="s1")

    out = edit_file(ws, "a.py", "x = 1", "x = 2", "s1")

    assert "replaced one occurrence" in out
    assert (ws.root / "a.py").read_text() == "x = 2\n"


def test_an_out_of_band_change_invalidates_the_read(ws) -> None:
    """Something else wrote to the file — a shell command, the kernel, the
    user. Editing against the stale copy would silently discard it."""
    import os

    from daimon_agent.tools.files import edit_file, read_file

    path = ws.root / "a.py"
    path.write_text("x = 1\n")
    read_file(ws, "a.py", session_id="s1")

    path.write_text("x = 1\ny = 2\n")
    os.utime(path, (0, 0))  # make the change unmistakable to stat()

    out = edit_file(ws, "a.py", "x = 1", "x = 2", "s1")

    assert "changed on disk" in out
    assert path.read_text() == "x = 1\ny = 2\n"  # the other change survives


def test_a_second_edit_after_the_first_is_allowed(ws) -> None:
    """A successful edit refreshes the stamp — otherwise the agent's own write
    would lock it out of the next one."""
    from daimon_agent.tools.files import edit_file, read_file

    (ws.root / "a.py").write_text("x = 1\ny = 2\n")
    read_file(ws, "a.py", session_id="s1")

    assert "replaced" in edit_file(ws, "a.py", "x = 1", "x = 9", "s1")
    assert "replaced" in edit_file(ws, "a.py", "y = 2", "y = 8", "s1")
    assert (ws.root / "a.py").read_text() == "x = 9\ny = 8\n"


def test_ambiguous_old_text_is_refused(ws) -> None:
    """Replacing the first of several is a coin flip about which one was meant."""
    from daimon_agent.tools.files import edit_file, read_file

    (ws.root / "a.py").write_text("call()\ncall()\n")
    read_file(ws, "a.py", session_id="s1")

    out = edit_file(ws, "a.py", "call()", "other()", "s1")

    assert "appears 2 times" in out
    assert (ws.root / "a.py").read_text() == "call()\ncall()\n"


def test_blind_overwrite_of_an_existing_file_is_refused(ws) -> None:
    from daimon_agent.tools.files import write_file

    (ws.root / "a.py").write_text("months of work\n")

    out = write_file(ws, "a.py", "clobbered", "s1")

    assert "Refusing to overwrite" in out
    assert (ws.root / "a.py").read_text() == "months of work\n"


def test_a_new_file_needs_no_prior_read(ws) -> None:
    from daimon_agent.tools.files import edit_file, write_file

    assert "Wrote" in write_file(ws, "new.py", "fresh\n", "s1")
    # And writing it counts as having seen it, so an immediate edit works.
    assert "replaced" in edit_file(ws, "new.py", "fresh", "changed", "s1")


def test_the_registry_is_per_session(ws) -> None:
    from daimon_agent.tools.files import edit_file, read_file

    (ws.root / "a.py").write_text("x = 1\n")
    read_file(ws, "a.py", session_id="s1")

    assert "have not read it" in edit_file(ws, "a.py", "x = 1", "x = 2", "s2")


# --- paging and line numbers --------------------------------------------------

def test_read_file_numbers_lines_and_pages(ws) -> None:
    from daimon_agent.tools.files import read_file

    (ws.root / "big.py").write_text("\n".join(f"line {i}" for i in range(1, 101)))

    page = read_file(ws, "big.py", offset="10", limit="3", session_id="s1")

    assert "  10→line 10" in page
    assert "  12→line 12" in page
    assert "line 13" not in page
    assert "offset=13" in page  # how to get the rest


def test_edit_accepts_text_quoted_back_with_its_line_numbers(ws) -> None:
    """The most natural thing for a model to do with a numbered read is paste
    the lines straight back. Without stripping the gutter that never matches."""
    from daimon_agent.tools.files import edit_file, read_file

    (ws.root / "a.py").write_text("alpha\nbeta\n")
    read_file(ws, "a.py", session_id="s1")

    out = edit_file(ws, "a.py", "   1→alpha", "   1→omega", "s1")

    assert "replaced one occurrence" in out
    assert (ws.root / "a.py").read_text() == "omega\nbeta\n"


def test_read_past_the_end_says_so(ws) -> None:
    from daimon_agent.tools.files import read_file

    (ws.root / "a.py").write_text("one\ntwo\n")
    assert "past the end" in read_file(ws, "a.py", offset="99", session_id="s1")
