"""Model binding — the hybrid Pro/Flash router.

- `.pro()`   — the main agent role (chat session, debugging, implementation).
               Temperature 0.0: a tool-calling agent needs determinism.
- `.flash()` — cheap bulk work (subagents, file ops, search, summarization).
               Temperature 0.0; a missed call is cheap to retry.
- `.judge()` — the SkillOpt optimizer's judge role. Temperature 0.7, and it
               is deliberately a *separate model instance* (the SkillOpt rule:
               optimizer is its own role, not the agent's).
"""

from __future__ import annotations

from langchain_deepseek import ChatDeepSeek

from .config import Settings


class ModelRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pro: ChatDeepSeek | None = None
        self._flash: ChatDeepSeek | None = None
        self._judge: ChatDeepSeek | None = None

    def _build(self, model: str, temperature: float) -> ChatDeepSeek:
        s = self._settings
        kwargs: dict = {
            "model": model,
            "api_key": s.api_key,
            "temperature": temperature,
            "max_tokens": s.max_tokens,
            "max_retries": s.max_retries,
            "timeout": s.request_timeout,
        }
        if s.api_base:  # None fails ChatDeepSeek's pydantic validation
            kwargs["api_base"] = s.api_base
        return ChatDeepSeek(**kwargs)

    def pro(self) -> ChatDeepSeek:
        if self._pro is None:
            self._pro = self._build(self._settings.model, self._settings.temperature)
        return self._pro

    def flash(self) -> ChatDeepSeek:
        if self._flash is None:
            self._flash = self._build(self._settings.resolved_flash_model, self._settings.temperature)
        return self._flash

    def judge(self) -> ChatDeepSeek:
        if self._judge is None:
            self._judge = self._build(self._settings.model, 0.7)
        return self._judge
