"""Chat-model construction across providers.

A model is named by a *spec*: either a bare model name (`deepseek-chat`), which
resolves against the configured default provider, or a `provider:model` pair
(`anthropic:claude-sonnet-5`). That single string is all the router, the config
file, and the env vars ever need to carry — which is what lets the pro role run
on a strong model while flash and the subagents stay on a cheap one.

Providers are imported lazily. `langchain-anthropic` is an optional extra, so a
DeepSeek-only install never pays for it and never sees an ImportError; asking
for an uninstalled provider fails with an instruction instead of a traceback.
"""

from __future__ import annotations

from typing import Any

DEFAULT_PROVIDER = "deepseek"
KNOWN_PROVIDERS = ("deepseek", "anthropic")

#: Provider → the pip extra that supplies it, for the error message.
_EXTRAS = {"anthropic": 'daimon-agent[anthropic]'}


class UnknownProvider(ValueError):
    pass


class ProviderNotInstalled(RuntimeError):
    pass


def parse_spec(spec: str, default_provider: str = DEFAULT_PROVIDER) -> tuple[str, str]:
    """Split a model spec into (provider, model).

    `"anthropic:claude-sonnet-5"` → `("anthropic", "claude-sonnet-5")`
    `"deepseek-chat"`             → `(default_provider, "deepseek-chat")`

    Only a known provider prefix counts as one — a colon inside a model name
    (some hosted models use them) is left alone rather than silently eaten.
    """
    if ":" in spec:
        head, _, tail = spec.partition(":")
        if head in KNOWN_PROVIDERS and tail:
            return head, tail
    return default_provider, spec


def provider_available(provider: str) -> bool:
    """True if the provider's package is importable. Used by /config to report
    what this install can actually run, without constructing anything."""
    import importlib.util

    module = {"deepseek": "langchain_deepseek", "anthropic": "langchain_anthropic"}.get(
        provider
    )
    if module is None:
        return False
    return importlib.util.find_spec(module) is not None


def build_chat_model(
    spec: str,
    *,
    temperature: float,
    settings: Any,
    streaming: bool = False,
) -> Any:
    """Construct a LangChain chat model for `spec`.

    `streaming` only turns on usage reporting for streamed calls — LangChain's
    OpenAI-compatible clients omit the usage block from a stream unless asked,
    and we would rather pay a token for the accounting than fly blind.
    """
    provider, model = parse_spec(spec, settings.provider)
    if provider not in KNOWN_PROVIDERS:
        raise UnknownProvider(
            f"unknown provider {provider!r} in model spec {spec!r} — "
            f"expected one of {', '.join(KNOWN_PROVIDERS)}"
        )
    if not provider_available(provider):
        extra = _EXTRAS.get(provider, "daimon-agent")
        raise ProviderNotInstalled(
            f"model spec {spec!r} needs the {provider} provider, which isn't "
            f"installed — run `uv sync --extra {provider}` (or pip install "
            f"'{extra}')"
        )

    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": settings.api_key,
            "temperature": temperature,
            "max_tokens": settings.max_tokens,
            "max_retries": settings.max_retries,
            "timeout": settings.request_timeout,
        }
        if streaming:
            # Without this the streamed response carries no usage block at all.
            kwargs["stream_usage"] = True
        if settings.api_base:  # None fails ChatDeepSeek's pydantic validation
            kwargs["api_base"] = settings.api_base
        return ChatDeepSeek(**kwargs)

    from langchain_anthropic import ChatAnthropic

    anthropic_kwargs: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": settings.max_tokens,
        "max_retries": settings.max_retries,
        "timeout": settings.request_timeout,
    }
    if settings.anthropic_api_key:
        anthropic_kwargs["api_key"] = settings.anthropic_api_key
    if settings.anthropic_api_base:
        anthropic_kwargs["base_url"] = settings.anthropic_api_base
    return ChatAnthropic(**anthropic_kwargs)


# --- what each provider offers -----------------------------------------------

#: Fallback list, used when there's no key to ask with or the network is down.
#: Deliberately short: it is a starting point for a picker, not a claim to be
#: current — providers ship models faster than a hard-coded list can track,
#: which is why `list_models` prefers asking the provider itself.
MODEL_CATALOG: dict[str, tuple[str, ...]] = {
    "deepseek": ("deepseek-v4-pro", "deepseek-v4-flash"),
    "anthropic": (
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-fable-5",
    ),
}

#: Stable names a provider accepts but doesn't list. DeepSeek's `/models`
#: returns only concrete names, so a picker built from it alone would omit the
#: very alias most installs are configured with.
MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "deepseek": ("deepseek-chat", "deepseek-reasoner"),
    "anthropic": (),
}

MODELS_TIMEOUT_S = 10.0


def provider_key(provider: str, settings: Any) -> str | None:
    """The configured key for a provider, from settings or the environment —
    both, because a key can arrive either way."""
    import os

    if provider == "anthropic":
        return getattr(settings, "anthropic_api_key", None) or os.environ.get(
            "ANTHROPIC_API_KEY"
        )
    return getattr(settings, "api_key", None) or os.environ.get("DEEPSEEK_API_KEY")


async def _fetch_models(provider: str, settings: Any, client: Any = None) -> list[str]:
    """Ask the provider what it serves. Empty list on any failure — the caller
    falls back to the catalogue rather than showing an error where a list of
    choices belongs."""
    import httpx

    key = provider_key(provider, settings)
    if not key:
        return []

    if provider == "deepseek":
        base = (getattr(settings, "api_base", None) or "https://api.deepseek.com").rstrip("/")
        url, headers = f"{base}/models", {"Authorization": f"Bearer {key}"}
    else:
        base = (
            getattr(settings, "anthropic_api_base", None) or "https://api.anthropic.com"
        ).rstrip("/")
        url = f"{base}/v1/models"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=MODELS_TIMEOUT_S)
    try:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            return []
        payload = response.json()
    except Exception:
        return []  # a picker that can't reach the network still needs options
    finally:
        if owns_client:
            await client.aclose()

    # Both providers answer with {"data": [{"id": ...}]}.
    return [
        str(item["id"])
        for item in (payload.get("data") or [])
        if isinstance(item, dict) and item.get("id")
    ]


async def list_models(
    provider: str, settings: Any, *, client: Any = None
) -> tuple[list[str], str]:
    """Every model worth offering for `provider`, and where the list came from.

    Live results first, then the aliases the provider accepts but doesn't list,
    then whatever is currently configured — a picker that can't reproduce the
    value already in use is a picker that silently changes your settings.
    """
    live = await _fetch_models(provider, settings, client)
    source = "live" if live else "catalog"
    models = list(live) or list(MODEL_CATALOG.get(provider, ()))

    for alias in MODEL_ALIASES.get(provider, ()):
        if alias not in models:
            models.append(alias)

    default_provider = getattr(settings, "provider", DEFAULT_PROVIDER)
    for spec in (getattr(settings, "model", ""), getattr(settings, "flash_model", "") or ""):
        if not spec:
            continue
        owner, name = parse_spec(spec, default_provider)
        if owner == provider and name not in models:
            models.append(name)

    return models, source


def supports_prompt_cache_control(spec: str, default_provider: str = DEFAULT_PROVIDER) -> bool:
    """True when the provider wants explicit `cache_control` markers on the
    prompt. Anthropic does; DeepSeek caches its prefix automatically, which is
    what `prompts.py`'s frozen-prefix layout is built around."""
    provider, _ = parse_spec(spec, default_provider)
    return provider == "anthropic"
