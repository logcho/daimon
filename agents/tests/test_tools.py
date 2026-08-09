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
