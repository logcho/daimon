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


def supports_prompt_cache_control(spec: str, default_provider: str = DEFAULT_PROVIDER) -> bool:
    """True when the provider wants explicit `cache_control` markers on the
    prompt. Anthropic does; DeepSeek caches its prefix automatically, which is
    what `prompts.py`'s frozen-prefix layout is built around."""
    provider, _ = parse_spec(spec, default_provider)
    return provider == "anthropic"
