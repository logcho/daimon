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
    assert s.resolved_skills_dir == s.vault_dir / "skills"
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
    assert s.resolved_skills_dir == Path("/tmp/v") / "skills"
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
