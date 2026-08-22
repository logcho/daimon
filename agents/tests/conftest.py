"""Shared fixtures: env isolation and tmp workspace/vault trees."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from daimon_agent.config import Settings


@pytest.fixture(autouse=True)
def restore_environment():
    """Put `os.environ` back after every test.

    `clean_env` only shields tests from a developer's *inherited* values. This
    guards the other direction: a test that writes to the environment mid-run —
    `POST /config` does exactly that, since setting a key has to take effect
    immediately — would otherwise leak into everything that follows and make
    results depend on test order.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A settings dict with every daimon-relevant key removed, so tests never
    inherit a developer's real .env values."""
    keys = [
        "DAIMON_MODEL", "DAIMON_FLASH_MODEL", "DAIMON_PROVIDER",
        "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE", "DAIMON_API_BASE",
        "ANTHROPIC_API_KEY", "ANTHROPIC_API_BASE",
        "DAIMON_VAULT_DIR", "DAIMON_WORKSPACE_DIR",
        "DAIMON_MEMORY_DB", "DAIMON_CHECKPOINTS_DB", "DAIMON_EVENTS_DB", "DAIMON_SKILLS_DIR",
        "DAIMON_GLOBAL_CONTEXT",
        "PINCHTAB_BASE", "PINCHTAB_TOKEN", "PORT", "DAIMON_LIVE_FRAMES", "TAVILY_API_KEY",
        "DAIMON_RECURSION_LIMIT", "DAIMON_MAX_STEPS", "DAIMON_COMPACTION_TOKENS",
        "DAIMON_INACTIVITY_TIMEOUT_S", "DAIMON_IDLE_TIMEOUT_S",
        "DAIMON_COMPACTION_CHARS", "DAIMON_CONTEXT_WINDOW", "DAIMON_PRICES",
        "GITHUB_TOKEN", "GH_TOKEN",
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
    """Settings pointed at a tmp vault/workspace with no model credentials.

    `DAIMON_SKILLS_DIR` is pinned explicitly: the skill library now defaults to
    `~/.daimon/skills`, which is a real directory belonging to whoever runs the
    tests. Without this, a test that saves a skill writes into their actual
    library — which happened, and is exactly the kind of thing a test suite
    must not be able to do.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    return Settings.from_env(
        {
            **clean_env,
            "DAIMON_VAULT_DIR": str(vault),
            "DAIMON_SKILLS_DIR": str(tmp_path / "skills"),
            # Same reasoning as the skills dir: without pinning it, every test
            # that builds a prompt would read the developer's own standing
            # instructions and behave differently on their machine than in CI.
            "DAIMON_GLOBAL_CONTEXT": str(tmp_path / "DAIMON.md"),
            "DAIMON_MEMORY_DB": str(tmp_path / "memory" / "daimon.db"),
            "DAIMON_CHECKPOINTS_DB": str(tmp_path / "memory" / "checkpoints.db"),
            "DAIMON_EVENTS_DB": str(tmp_path / "memory" / "events.db"),
        }
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A bare tmp directory acting as the confined workspace."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes").mkdir()
    return root
