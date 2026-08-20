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
    # Either may be a bare name (resolved against `provider`) or an explicit
    # `provider:model` spec — that is how the main agent runs on a strong model
    # while subagents and summarization stay on a cheap one.
    model: str = "deepseek-chat"
    flash_model: str | None = None
    #: Default provider for bare model names.
    provider: str = "deepseek"
    api_key: str | None = None  # DeepSeek
    api_base: str | None = None
    anthropic_api_key: str | None = None
    anthropic_api_base: str | None = None

    # Workspace / data dirs.
    vault_dir: Path = Path("./vault")
    workspace_dir: Path | None = None  # defaults to vault_dir
    memory_db: Path | None = None  # defaults to resolved_workspace_dir/.daimon/memory/daimon.db
    checkpoints_db: Path | None = None  # defaults to resolved_workspace_dir/.daimon/memory/checkpoints.db
    skills_dir: Path | None = None  # defaults to vault_dir / "skills"
    global_context: Path | None = None  # defaults to ~/.daimon/DAIMON.md

    #: Seconds a server may sit with no active turns before it self-terminates
    #: (SIGTERM). A per-workspace server has nothing else watching it once the
    #: terminal that spawned it closes. 0 disables auto-shutdown.
    idle_timeout_s: float = 2700.0

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
    #: Graph steps before LangGraph raises. This is a *loop guard*, not a
    #: task-length budget — real looping is caught by guardrails.py. Hitting it
    #: no longer ends the turn; the turn continues from the checkpoint.
    recursion_limit: int = 150
    #: Total steps across all continuations before the agent stops and asks
    #: whether to keep going. The actual ceiling on an unattended run.
    max_steps_per_turn: int = 600
    live_frames: bool = False
    # Post-turn reflection (tool-using turns only) and token-threshold
    # compaction (main agent only, summarized via flash).
    reflect: bool = True
    #: Compaction fires above this many *tokens* of conversation. Real usage
    #: numbers from the last model call drive it; `compaction_chars` is only
    #: the cold-start estimate for the very first call of a session, before
    #: any usage has been reported.
    compaction_tokens: int = 60000
    compaction_chars: int = 40000
    #: Denominator for the context-usage indicator. Not a hard limit — the
    #: provider enforces the real one; this is what "34% full" is a share of.
    context_window: int = 128000

    # Search.
    tavily_api_key: str | None = None

    @property
    def resolved_workspace_dir(self) -> Path:
        return self.workspace_dir or self.vault_dir

    @property
    def resolved_memory_db(self) -> Path:
        """Per-workspace by default — two different projects using the same
        default session name ("cli") must not share one memory index.
        `DAIMON_MEMORY_DB` still overrides it."""
        return self.memory_db or (self.resolved_workspace_dir / ".daimon" / "memory" / "daimon.db")

    @property
    def resolved_checkpoints_db(self) -> Path:
        """Per-workspace by default — this is what keeps two projects both
        using the default session name "cli" in two different DBs instead of
        one shared row. `DAIMON_CHECKPOINTS_DB` still overrides it."""
        return self.checkpoints_db or (self.resolved_workspace_dir / ".daimon" / "memory" / "checkpoints.db")

    @property
    def resolved_skills_dir(self) -> Path:
        """The global library — genuinely global.

        It used to default to `vault_dir / "skills"`, and `vault_dir` defaults
        to the *relative* `./vault`, resolved against whatever cwd the server
        was spawned with. For an installed `daimon` that is the directory you
        happened to launch from, so the skill library moved with you and the
        app (which resolves the vault differently again) never saw it at all.

        `~/.daimon/skills` depends on nothing — not cwd, not the workspace, not
        the vault, not the app's data dir — which is what "always available"
        requires. `DAIMON_SKILLS_DIR` still overrides it.
        """
        return self.skills_dir or (Path.home() / ".daimon" / "skills")

    @property
    def resolved_global_context(self) -> Path:
        """Standing instructions that follow the user everywhere.

        Beside the global skill library, and global for the same reason: it must
        not depend on cwd, the workspace, or the vault. `DAIMON_GLOBAL_CONTEXT`
        overrides it — which is also what keeps a test run from reading the
        developer's own file.
        """
        return self.global_context or (Path.home() / ".daimon" / "DAIMON.md")

    @property
    def registry_cache_dir(self) -> Path:
        """Cached registry/GitHub responses. Lives beside the databases because
        it is derived data — deleting it costs a re-fetch, nothing else."""
        return self.resolved_memory_db.parent / "registry-cache"

    @property
    def project_skills_dir(self) -> Path:
        """Skills that live with the code. Committing `.daimon/skills/` shares
        a procedure with everyone who works on the repo, which a personal
        library can't do."""
        return self.resolved_workspace_dir / ".daimon" / "skills"

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
            provider=get("DAIMON_PROVIDER") or "deepseek",
            api_key=get("DEEPSEEK_API_KEY"),
            api_base=api_base,
            anthropic_api_key=get("ANTHROPIC_API_KEY"),
            anthropic_api_base=get("ANTHROPIC_API_BASE"),
            vault_dir=Path(get("DAIMON_VAULT_DIR") or "./vault"),
            workspace_dir=Path(get("DAIMON_WORKSPACE_DIR")) if get("DAIMON_WORKSPACE_DIR") else None,
            memory_db=Path(get("DAIMON_MEMORY_DB")) if get("DAIMON_MEMORY_DB") else None,
            checkpoints_db=Path(get("DAIMON_CHECKPOINTS_DB")) if get("DAIMON_CHECKPOINTS_DB") else None,
            skills_dir=Path(get("DAIMON_SKILLS_DIR")) if get("DAIMON_SKILLS_DIR") else None,
            global_context=(
                Path(get("DAIMON_GLOBAL_CONTEXT")) if get("DAIMON_GLOBAL_CONTEXT") else None
            ),
            pinchtab_base=get("PINCHTAB_BASE") or "http://127.0.0.1:9867",
            pinchtab_token=get("PINCHTAB_TOKEN"),
            port=int(get("PORT") or "4711"),
            idle_timeout_s=float(get("DAIMON_IDLE_TIMEOUT_S") or "2700"),
            recursion_limit=int(get("DAIMON_RECURSION_LIMIT") or "150"),
            max_steps_per_turn=int(get("DAIMON_MAX_STEPS") or "600"),
            live_frames=get_bool("DAIMON_LIVE_FRAMES", False),
            reflect=get_bool("DAIMON_REFLECT", True),
            compaction_tokens=int(get("DAIMON_COMPACTION_TOKENS") or "60000"),
            compaction_chars=int(get("DAIMON_COMPACTION_CHARS") or "40000"),
            context_window=int(get("DAIMON_CONTEXT_WINDOW") or "128000"),
            tavily_api_key=get("TAVILY_API_KEY"),
        )
