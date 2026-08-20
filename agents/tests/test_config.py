"""Settings loading: defaults, env overrides, resolved fallbacks."""

from __future__ import annotations

import os
from pathlib import Path

from daimon_agent.config import Settings


def test_defaults() -> None:
    s = Settings.from_env({})
    assert s.model == "deepseek-chat"
    assert s.flash_model is None
    assert s.resolved_flash_model == "deepseek-chat"  # falls back to pro id
    assert s.vault_dir == Path("./vault")
    assert s.resolved_workspace_dir == s.vault_dir
    # No longer vault-relative: the library is global so it can't move
    # with the cwd. See test_skills_default_to_a_user_level_directory.
    assert s.resolved_skills_dir == Path.home() / ".daimon" / "skills"
    # Memory/checkpoints DO follow the workspace by default — that's what
    # keeps two different projects' conversation histories apart. See
    # test_memory_and_checkpoints_default_under_the_workspace.
    assert s.resolved_memory_db == s.resolved_workspace_dir / ".daimon" / "memory" / "daimon.db"
    assert s.resolved_checkpoints_db == s.resolved_workspace_dir / ".daimon" / "memory" / "checkpoints.db"
    assert s.port == 4711
    assert s.temperature == 0.0
    assert s.inactivity_timeout_s == 60.0


def test_env_overrides() -> None:
    s = Settings.from_env(
        {
            "DAIMON_MODEL": "deepseek-v4-pro",
            "DAIMON_FLASH_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_API_KEY": "sk-test",
            "DAIMON_VAULT_DIR": "/tmp/v",
            "DAIMON_WORKSPACE_DIR": "/tmp/w",
            "PORT": "9999",
        }
    )
    assert s.model == "deepseek-v4-pro"
    assert s.resolved_flash_model == "deepseek-v4-flash"
    assert s.api_key == "sk-test"
    assert s.resolved_workspace_dir == Path("/tmp/w")
    # Independent of the vault by design — a vault move must not take the
    # skill library with it.
    assert s.resolved_skills_dir == Path.home() / ".daimon" / "skills"
    assert s.port == 9999


def test_flash_model_falls_back_to_pro() -> None:
    s = Settings.from_env({"DAIMON_MODEL": "deepseek-v4-pro"})
    assert s.resolved_flash_model == "deepseek-v4-pro"


def test_api_base_falls_through_to_deepseek_env() -> None:
    s = Settings.from_env({"DEEPSEEK_API_BASE": "https://proxy.example.com/v1"})
    assert s.api_base == "https://proxy.example.com/v1"
    # Explicit DAIMON_API_BASE wins.
    s2 = Settings.from_env(
        {"DEEPSEEK_API_BASE": "https://proxy.example.com/v1", "DAIMON_API_BASE": "https://direct.example.com/v1"}
    )
    assert s2.api_base == "https://direct.example.com/v1"


def test_live_frames_bool_parsing() -> None:
    assert Settings.from_env({"DAIMON_LIVE_FRAMES": "0"}).live_frames is False
    assert Settings.from_env({"DAIMON_LIVE_FRAMES": "true"}).live_frames is True


def test_real_env_wins_over_dotenv_file(tmp_path, monkeypatch) -> None:
    """The file-merge path (env=None reads cwd/.env): a real env var must
    never be clobbered by a .env value — the app spawns the server with a
    specific PORT, and .env (e.g. PORT=4711) must not override it."""
    (tmp_path / ".env").write_text("PORT=4711\nDAIMON_MODEL=deepseek-chat\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PORT", "59238")
    monkeypatch.setenv("DAIMON_MODEL", "deepseek-v4-pro")
    s = Settings.from_env()
    assert s.port == 59238
    assert s.model == "deepseek-v4-pro"
    # And a key absent from the real env still comes from .env.
    monkeypatch.delenv("DAIMON_MODEL")
    assert Settings.from_env().model == "deepseek-chat"


# --- the global skill library ------------------------------------------------

def test_skills_default_to_a_user_level_directory(clean_env) -> None:
    """It used to default under the vault, which resolves against the server's
    cwd — so for an installed `daimon` the library followed whatever directory
    you launched from, and the app looked somewhere else again."""
    from pathlib import Path

    settings = Settings.from_env({**clean_env, "DAIMON_VAULT_DIR": "./vault"})
    assert settings.resolved_skills_dir == Path.home() / ".daimon" / "skills"
    # And it does not move when the vault does.
    moved = Settings.from_env({**clean_env, "DAIMON_VAULT_DIR": "/somewhere/else"})
    assert moved.resolved_skills_dir == settings.resolved_skills_dir


def test_skills_dir_can_be_overridden(clean_env) -> None:
    from pathlib import Path

    settings = Settings.from_env({**clean_env, "DAIMON_SKILLS_DIR": "/tmp/my-skills"})
    assert settings.resolved_skills_dir == Path("/tmp/my-skills")


def test_project_skills_still_follow_the_workspace(clean_env, tmp_path) -> None:
    settings = Settings.from_env({**clean_env, "DAIMON_WORKSPACE_DIR": str(tmp_path)})
    assert settings.project_skills_dir == tmp_path / ".daimon" / "skills"


# --- per-workspace memory/checkpoints ----------------------------------------

def test_memory_and_checkpoints_default_under_the_workspace(clean_env, tmp_path) -> None:
    """Two different projects using the same default session name ("cli")
    must not land in the same DB — the default has to move with the
    workspace, not stay anchored to wherever the server process happens to
    run from."""
    settings = Settings.from_env({**clean_env, "DAIMON_WORKSPACE_DIR": str(tmp_path)})
    assert settings.resolved_memory_db == tmp_path / ".daimon" / "memory" / "daimon.db"
    assert settings.resolved_checkpoints_db == tmp_path / ".daimon" / "memory" / "checkpoints.db"

    other = Settings.from_env({**clean_env, "DAIMON_WORKSPACE_DIR": str(tmp_path / "other")})
    assert other.resolved_checkpoints_db != settings.resolved_checkpoints_db


def test_memory_and_checkpoints_can_be_overridden(clean_env, tmp_path) -> None:
    settings = Settings.from_env(
        {
            **clean_env,
            "DAIMON_WORKSPACE_DIR": str(tmp_path),
            "DAIMON_MEMORY_DB": "/tmp/custom-memory.db",
            "DAIMON_CHECKPOINTS_DB": "/tmp/custom-checkpoints.db",
        }
    )
    assert settings.resolved_memory_db == Path("/tmp/custom-memory.db")
    assert settings.resolved_checkpoints_db == Path("/tmp/custom-checkpoints.db")
