"""The `/setup` wizard — the guided path from nothing to a working agent.

`/setup` used to print the current configuration, which is what `/config` does
now. This is the thing the name promises: pick a provider, give it a key, choose
the models, done.

It is a plain step machine rather than anything clever about terminals. Each
step produces an ask payload (the same shape the agent's own questions use, so
the TUI needs no second mechanism) and consumes the answer, returning the lines
to print. Everything is written through `POST /config`, so the server's
validation — provider installed, key present — guards this path exactly as it
guards every other.

Keeping the flow here, free of prompt_toolkit, is what lets the whole sequence
be tested by calling `start()` and `answer()` in order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import client
from ..providers import KNOWN_PROVIDERS
from .render import BLUE, BOLD, DIM, GREEN, RED, RESET

#: Ask ids are prefixed so the TUI can tell a local confirmation from a
#: suspended turn without knowing what either is about.
PREFIX = "setup:"

_ENV_HINT = {
    "deepseek": "https://platform.deepseek.com/api_keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
}


def _ask(
    step: str,
    question: str,
    options: list[dict],
    *,
    header: str = "Setup",
    secret: bool = False,
) -> dict:
    payload: dict[str, Any] = {
        "type": "ask",
        "id": f"{PREFIX}{step}",
        "kind": "question",
        "header": header,
        "question": question,
        "options": options,
        "multi_select": False,
    }
    if secret:
        payload["secret"] = True
    return payload


@dataclass
class SetupFlow:
    """One run of the wizard. `start()` and `answer()` each return
    `(lines, ask)` — lines to print now, and the next question or None when
    it's over."""

    http: Any = None
    port: int = 0
    config: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    provider: str = ""
    main_model: str = ""
    done: bool = False

    # --- steps --------------------------------------------------------------

    async def start(self) -> tuple[list[str], dict | None]:
        self.config = await client.get_config(self.http, self.port)
        if self.config.get("error"):
            return [f"  {RED}{self.config['error']}{RESET}"], None

        providers = self.config.get("providers") or {}
        options = []
        for name in KNOWN_PROVIDERS:
            state = providers.get(name) or {}
            if not state.get("installed"):
                note = f"not installed — uv sync --extra {name}"
            elif state.get("key_configured"):
                note = "key already set"
            else:
                note = "needs an API key"
            options.append({"label": name, "description": note})

        lines = ["", f"{DIM}── setup{RESET}"]
        return lines, _ask("provider", "Which provider should the agent use?", options)

    async def answer(self, step: str, value: str) -> tuple[list[str], dict | None]:
        handler = getattr(self, f"_on_{step}", None)
        if handler is None:
            return [], None
        return await handler(value)

    async def _on_provider(self, value: str) -> tuple[list[str], dict | None]:
        self.provider = value.strip().lower()
        state = (self.config.get("providers") or {}).get(self.provider) or {}

        if not state.get("installed"):
            # Say it here rather than letting the first model call fail: the
            # fix is a package install, not anything inside the app.
            return [
                "",
                f"  {RED}✖{RESET} the {self.provider} provider isn't installed",
                f"  {DIM}install it and run /setup again:{RESET}"
                f"  {BOLD}uv sync --extra {self.provider}{RESET}",
            ], None

        if state.get("key_configured"):
            lines = [f"  {DIM}{self.provider} already has a key{RESET}"]
            return await self._ask_models(lines)

        return [], _ask(
            "key",
            f"Paste your {self.provider} API key "
            f"({_ENV_HINT.get(self.provider, '')})",
            [],
            secret=True,
        )

    async def _on_key(self, value: str) -> tuple[list[str], dict | None]:
        key = value.strip()
        if not key:
            return ["", f"  {DIM}⊘ no key entered — setup cancelled{RESET}"], None

        result = await client.update_config(self.http, self.port, {"key": key})
        if result.get("error"):
            return ["", f"  {RED}{result['error']}{RESET}"], None

        # Re-read: the key changes what the model list can be fetched with.
        self.config = await client.get_config(self.http, self.port)
        return await self._ask_models([f"  {GREEN}✓{RESET} key saved to .env"])

    async def _ask_models(self, lines: list[str]) -> tuple[list[str], dict | None]:
        self.models = await client.list_models(self.http, self.port)
        entry = (self.models or {}).get(self.provider) or {}
        names = entry.get("models") or []
        if not names:
            return lines + [
                f"  {DIM}couldn't list {self.provider} models — set one later with"
                f" /setup or in the app{RESET}"
            ], None
        if entry.get("source") == "catalog":
            lines.append(
                f"  {DIM}(couldn't reach {self.provider}; showing known models){RESET}"
            )
        return lines, _ask(
            "main_model",
            "Which model should the main agent use?",
            [{"label": n, "description": ""} for n in names[:9]],
        )

    async def _on_main_model(self, value: str) -> tuple[list[str], dict | None]:
        self.main_model = self._spec(value.strip())
        result = await client.update_config(
            self.http, self.port, {"model": self.main_model}
        )
        if result.get("error"):
            return ["", f"  {RED}{result['error']}{RESET}"], None

        names = ((self.models or {}).get(self.provider) or {}).get("models") or []
        options = [
            {"label": "same as main", "description": self.main_model},
            *({"label": n, "description": ""} for n in names[:8]),
        ]
        return [], _ask(
            "flash_model",
            "And for sub-agents and summarising? (cheaper is fine here)",
            options,
        )

    async def _on_flash_model(self, value: str) -> tuple[list[str], dict | None]:
        choice = value.strip()
        spec = self.main_model if choice.startswith("same as") else self._spec(choice)
        result = await client.update_config(self.http, self.port, {"flash_model": spec})
        if result.get("error"):
            return ["", f"  {RED}{result['error']}{RESET}"], None

        self.done = True
        return [
            "",
            f"  {GREEN}✓{RESET} {BOLD}ready{RESET}",
            f"  {DIM}main{RESET}   {self.main_model}",
            f"  {DIM}flash{RESET}  {spec}",
            "",
            f"  {DIM}ask me anything — or{RESET} {BLUE}/config{RESET}"
            f" {DIM}to see the rest of the settings{RESET}",
        ], None

    def _spec(self, model: str) -> str:
        """Qualify the model with its provider unless it's the default one, so
        `anthropic:claude-sonnet-5` is what gets stored and the router knows
        where to send it."""
        default = self.config.get("provider") or "deepseek"
        return model if self.provider == default else f"{self.provider}:{model}"


def needs_setup(config: dict) -> bool:
    """True when no provider has a key — nothing can run, so a fresh install
    should be walked through rather than dropped at an empty prompt."""
    providers = (config or {}).get("providers") or {}
    if not providers:
        return not config.get("api_key_configured", False)
    return not any(state.get("key_configured") for state in providers.values())
