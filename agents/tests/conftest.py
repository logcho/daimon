"""Shared fixtures: env isolation and tmp workspace/vault trees."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from daimon_agent.config import Settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A settings dict with every daimon-relevant key removed, so tests never
    inherit a developer's real .env values."""
    keys = [
        "DAIMON_MODEL", "DAIMON_FLASH_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE",
        "DAIMON_API_BASE", "DAIMON_VAULT_DIR", "DAIMON_WORKSPACE_DIR",
        "DAIMON_MEMORY_DB", "DAIMON_CHECKPOINTS_DB", "DAIMON_SKILLS_DIR",
        "PINCHTAB_BASE", "PINCHTAB_TOKEN", "PORT", "DAIMON_LIVE_FRAMES", "TAVILY_API_KEY",
    ]
    env: dict[str, str] = {}
    for key in keys:
        if key in os.environ:
            env[key] = os.environ.pop(key)
            monkeypatch.setenv(key, env[key])
        else:
            monkeypatch.delenv(key, raising=False)
    return env


@pytest.fixture
def settings(clean_env: dict[str, str], tmp_path: Path) -> Settings:
    """Settings pointed at a tmp vault/workspace with no model credentials."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return Settings.from_env(
        {
            **clean_env,
            "DAIMON_VAULT_DIR": str(vault),
            "DAIMON_MEMORY_DB": str(tmp_path / "memory" / "daimon.db"),
            "DAIMON_CHECKPOINTS_DB": str(tmp_path / "memory" / "checkpoints.db"),
        }
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A bare tmp directory acting as the confined workspace."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes").mkdir()
    return root
