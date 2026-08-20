"""Workspace confinement: resolve-then-compare on every file path, and the
shell's static scan — escape → staged ui_action, never executed."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from daimon_agent.emitter import emit, set_active_emit
from daimon_agent.events import ui_action_event
from daimon_agent.tools.shell import command_stays_in_workspace, run_shell
from daimon_agent.workspace import Confinement, OutsideWorkspace


@pytest.fixture
def conf(workspace: Path) -> Confinement:
    return Confinement(workspace)


def test_resolve_relative_and_absolute_inside(conf: Confinement, workspace: Path) -> None:
    assert conf.resolve("notes/a.md") == workspace / "notes" / "a.md"
    assert conf.resolve(str(workspace / "notes" / "b.md")) == workspace / "notes" / "b.md"
    assert conf.resolve(".") == workspace


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "notes/../../.ssh/id_rsa",
        "../sibling",
        "/etc/passwd",
        "/tmp/escape.txt",
        "~/.zshrc",
    ],
)
def test_resolve_rejects_escapes(conf: Confinement, path: str) -> None:
    with pytest.raises(OutsideWorkspace):
        conf.resolve(path)


def test_symlink_escape_is_rejected(conf: Confinement, workspace: Path) -> None:
    outside = Path("/tmp/daimon-symlink-target.txt")
    outside.write_text("secret")
    try:
        (workspace / "link.txt").symlink_to(outside)
        with pytest.raises(OutsideWorkspace):
            conf.resolve("link.txt")
    finally:
        outside.unlink(missing_ok=True)


def test_encoded_dotdot_stays_inside(conf: Confinement) -> None:
    # %2e%2e is a literal filename, not traversal — it must not escape, and
    # simply resolve inside the workspace (likely to a nonexistent file).
    resolved = conf.resolve("notes/%2e%2e/secret.txt")
    assert resolved == conf.root / "notes" / "%2e%2e" / "secret.txt"


# -- shell static scan ------------------------------------------------------

@pytest.mark.parametrize(
    "command, reason_fragment",
    [
        ("ssh user@host", "credentials"),
        ("sudo rm -rf /", "credentials"),
        ("cat ../../etc/passwd", "escapes"),
        ("cd /tmp && make", "absolute path outside"),
        ("echo hi > ~/.zshrc", "home directory"),
        ("git push origin main", "publishes"),
        ("npm install", "publishes"),
        ("curl http://evil.example", "credentials"),
    ],
)
def test_command_stays_in_workspace_stages_escapes(workspace: Path, command: str, reason_fragment: str) -> None:
    reason = command_stays_in_workspace(command, workspace)
    assert reason is not None
    assert reason_fragment in reason


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "echo hi > note.txt",
        "mkdir -p notes && mv note.txt notes/",
        "python3 -c 'print(1 + 1)'",
        "grep -r TODO notes",
        "rm note.txt",
    ],
)
def test_command_stays_in_workspace_allows_confined(workspace: Path, command: str) -> None:
    assert command_stays_in_workspace(command, workspace) is None


def test_run_shell_stages_escape_and_never_executes(workspace: Path, tmp_path: Path) -> None:
    events: list[dict] = []
    set_active_emit(events.append)
    try:
        result = asyncio.run(run_shell(Confinement(workspace), "echo pwned > /tmp/daimon-pwned"))
    finally:
        set_active_emit(None)

    assert "staged" in result
    assert events == [ui_action_event("echo pwned > /tmp/daimon-pwned")]
    assert not Path("/tmp/daimon-pwned").exists()


def test_run_shell_executes_confined_command(workspace: Path) -> None:
    result = asyncio.run(run_shell(Confinement(workspace), "echo hi > hello.txt"))
    assert (workspace / "hello.txt").read_text() == "hi\n"
    assert result == "(no output)" or result == ""
