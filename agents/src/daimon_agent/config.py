"""Configuration — the single source of env-name truth.

Settings load from, in order of precedence: real environment variables >
`.daimon/pinchtab.env` (written by scripts/setup-pinchtab.sh; only
PINCHTAB_* keys are taken from it) > `.env` in the project root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    # Models. `model` is the Pro (main) role; `flash_model` defaults to it.
    model: str = "deepseek-chat"
    flash_model: str | None = None
    api_key: str | None = None
    api_base: str | None = None

    # Workspace / data dirs.
    vault_dir: Path = Path("./vault")
    workspace_dir: Path | None = None  # defaults to vault_dir
    memory_db: Path = Path("./memory/daimon.db")
    checkpoints_db: Path = Path("./memory/checkpoints.db")
    skills_dir: Path | None = None  # defaults to vault_dir / "skills"

    # PinchTab browser sidecar.
    pinchtab_base: str = "http://127.0.0.1:9867"
    pinchtab_token: str | None = None

    # HTTP server.
    port: int = 4711

    # Model call parameters.
    temperature: float = 0.0
    max_tokens: int = 4096
    max_retries: int = 2
    request_timeout: float = 60.0

    # Turn loop.
    inactivity_timeout_s: float = 60.0
    live_frames: bool = False
    # Post-turn reflection (tool-using turns only) and token-threshold
    # compaction (main agent only, summarized via flash).
    reflect: bool = True
    compaction_chars: int = 40000

    # Search.
    tavily_api_key: str | None = None

    @property
    def resolved_workspace_dir(self) -> Path:
        return self.workspace_dir or self.vault_dir

    @property
    def resolved_skills_dir(self) -> Path:
        return self.skills_dir or (self.vault_dir / "skills")

    @property
    def resolved_flash_model(self) -> str:
        return self.flash_model or self.model

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        """Load settings from environment variables (see module doc for order)."""
        if env is None:
            merged: dict[str, str] = {}
            # Real environment first — wins over every file. The .env files
            # below only fill keys the real environment doesn't already set
            # (a plain dict.update would clobber, e.g. the app spawning the
            # server with a specific PORT).
            merged.update(os.environ)
            # PinchTab env file written by setup-pinchtab.sh goes next —
            # it knows the actual port/token of the running instance, so it
            # must override .env defaults (don't check `k not in merged`).
            pinchtab_env = dotenv_values(Path.cwd() / ".daimon" / "pinchtab.env")
            merged.update(
                {
                    k: v
                    for k, v in pinchtab_env.items()
                    if v is not None and k.startswith("PINCHTAB_")
                }
            )
            # Project-root .env last — fills in defaults for keys not already
            # set by the real environment or the PinchTab env file.
            root_env = dotenv_values(Path.cwd() / ".env")
            merged.update(
                {k: v for k, v in root_env.items() if v is not None and k not in merged}
            )
        else:
            merged = dict(env)

        def get(key: str, default: str | None = None) -> str | None:
            return merged.get(key) if merged.get(key) not in (None, "") else default

        def get_bool(key: str, default: bool) -> bool:
            raw = get(key)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        api_base = get("DAIMON_API_BASE") or get("DEEPSEEK_API_BASE")

        return cls(
            model=get("DAIMON_MODEL") or "deepseek-chat",
            flash_model=get("DAIMON_FLASH_MODEL"),
            api_key=get("DEEPSEEK_API_KEY"),
            api_base=api_base,
            vault_dir=Path(get("DAIMON_VAULT_DIR") or "./vault"),
            workspace_dir=Path(get("DAIMON_WORKSPACE_DIR")) if get("DAIMON_WORKSPACE_DIR") else None,
            memory_db=Path(get("DAIMON_MEMORY_DB") or "./memory/daimon.db"),
            checkpoints_db=Path(get("DAIMON_CHECKPOINTS_DB") or "./memory/checkpoints.db"),
            skills_dir=Path(get("DAIMON_SKILLS_DIR")) if get("DAIMON_SKILLS_DIR") else None,
            pinchtab_base=get("PINCHTAB_BASE") or "http://127.0.0.1:9867",
            pinchtab_token=get("PINCHTAB_TOKEN"),
            port=int(get("PORT") or "4711"),
            live_frames=get_bool("DAIMON_LIVE_FRAMES", False),
            reflect=get_bool("DAIMON_REFLECT", True),
            compaction_chars=int(get("DAIMON_COMPACTION_CHARS") or "40000"),
            tavily_api_key=get("TAVILY_API_KEY"),
        )
