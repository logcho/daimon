"""Model binding — the hybrid Pro/Flash router.

- `.pro()`   — the main agent role (chat session, debugging, implementation).
               Temperature 0.0: a tool-calling agent needs determinism.
- `.flash()` — cheap bulk work (subagents, file ops, search, summarization).
               Temperature 0.0; a missed call is cheap to retry.
- `.judge()` — the SkillOpt optimizer's judge role. Temperature 0.7, and it
               is deliberately a *separate model instance* (the SkillOpt rule:
               optimizer is its own role, not the agent's).

Each role names its model with a spec (`providers.parse_spec`), so the roles
can sit on different providers — a strong pro model with cheap flash subagents
is the whole point of keeping them separate.
"""

from __future__ import annotations

from typing import Any

from .config import Settings
from .providers import build_chat_model, parse_spec


class ModelRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Keyed by (spec, temperature, streaming): a role asked for streaming
        # and the same role asked for a plain call are different clients, and
        # building one is not free.
        self._cache: dict[tuple[str, float, bool], Any] = {}

    def _build(self, spec: str, temperature: float, streaming: bool = False) -> Any:
        key = (spec, temperature, streaming)
        if key not in self._cache:
            self._cache[key] = build_chat_model(
                spec,
                temperature=temperature,
                settings=self._settings,
                streaming=streaming,
            )
        return self._cache[key]

    # --- roles ---------------------------------------------------------------

    def pro(self, *, streaming: bool = False) -> Any:
        return self._build(self._settings.model, self._settings.temperature, streaming)

    def flash(self, *, streaming: bool = False) -> Any:
        return self._build(
            self._settings.resolved_flash_model, self._settings.temperature, streaming
        )

    def judge(self) -> Any:
        return self._build(self._settings.model, 0.7)

    def for_role(self, role: str, *, streaming: bool = False) -> Any:
        """The model a graph role should use. `role` is "pro" or "flash"."""
        return self.pro(streaming=streaming) if role == "pro" else self.flash(streaming=streaming)

    # --- introspection -------------------------------------------------------

    def spec_for_role(self, role: str) -> str:
        return (
            self._settings.model
            if role == "pro"
            else self._settings.resolved_flash_model
        )

    def model_name(self, role: str) -> str:
        """The bare model name for a role, without the provider prefix — what
        the pricing table and the status bar want."""
        return parse_spec(self.spec_for_role(role), self._settings.provider)[1]
