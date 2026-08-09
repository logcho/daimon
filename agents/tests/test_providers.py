"""The provider abstraction: spec parsing, lazy imports, and the per-role
split that lets a strong main model sit alongside cheap sub-agents."""

from __future__ import annotations

from dataclasses import replace

import pytest

from daimon_agent.model import ModelRouter
from daimon_agent.providers import (
    ProviderNotInstalled,
    UnknownProvider,
    build_chat_model,
    parse_spec,
    provider_available,
    supports_prompt_cache_control,
)


def test_bare_name_uses_the_default_provider() -> None:
    assert parse_spec("deepseek-chat") == ("deepseek", "deepseek-chat")
    assert parse_spec("claude-sonnet-5", "anthropic") == ("anthropic", "claude-sonnet-5")


def test_explicit_prefix_wins() -> None:
    assert parse_spec("anthropic:claude-sonnet-5") == ("anthropic", "claude-sonnet-5")


def test_a_colon_that_is_not_a_provider_is_left_alone() -> None:
    """Some hosted model names contain colons; eating one silently would send
    a request for the wrong model."""
    assert parse_spec("org/model:v2") == ("deepseek", "org/model:v2")
    assert parse_spec("deepseek:") == ("deepseek", "deepseek:")


def test_deepseek_is_available_and_anthropic_is_optional() -> None:
    assert provider_available("deepseek") is True
    assert provider_available("nonsense") is False


def test_unknown_provider_is_rejected(settings) -> None:
    with pytest.raises(UnknownProvider, match="unknown provider"):
        build_chat_model(
            "deepseek-chat",
            temperature=0.0,
            settings=replace(settings, provider="wizard"),
        )


def test_missing_provider_package_says_how_to_install_it(settings, monkeypatch) -> None:
    """The two failure modes — package missing vs. key missing — are different
    problems, so the message names the one that happened."""
    monkeypatch.setattr(
        "daimon_agent.providers.provider_available", lambda name: name != "anthropic"
    )
    with pytest.raises(ProviderNotInstalled, match="uv sync --extra anthropic"):
        build_chat_model("anthropic:claude-sonnet-5", temperature=0.0, settings=settings)


def test_deepseek_model_is_built_from_settings(settings) -> None:
    s = replace(settings, api_key="sk-test", max_tokens=1234)
    model = build_chat_model("deepseek-chat", temperature=0.0, settings=s)
    assert model.model_name == "deepseek-chat"
    assert model.max_tokens == 1234


def test_streaming_turns_on_usage_reporting(settings) -> None:
    """An OpenAI-compatible stream omits usage entirely unless asked, which
    would leave every streamed call uncounted."""
    s = replace(settings, api_key="sk-test")
    assert build_chat_model("deepseek-chat", temperature=0.0, settings=s).stream_usage is not True
    streamed = build_chat_model("deepseek-chat", temperature=0.0, settings=s, streaming=True)
    assert streamed.stream_usage is True


def test_cache_control_is_provider_specific() -> None:
    """DeepSeek caches its prefix automatically; Anthropic needs explicit
    markers. Only one of them wants the prompt annotated."""
    assert supports_prompt_cache_control("deepseek-chat") is False
    assert supports_prompt_cache_control("anthropic:claude-sonnet-5") is True


# --- the router --------------------------------------------------------------

def test_router_splits_roles_across_providers(settings) -> None:
    """The point of keeping pro and flash separate: a strong main model with
    cheap sub-agents."""
    s = replace(settings, model="anthropic:claude-sonnet-5", flash_model="deepseek-chat")
    router = ModelRouter(s)
    assert router.model_name("pro") == "claude-sonnet-5"
    assert router.model_name("flash") == "deepseek-chat"
    assert router.spec_for_role("pro") == "anthropic:claude-sonnet-5"


def test_router_caches_per_spec_and_streaming_mode(settings) -> None:
    s = replace(settings, api_key="sk-test")
    router = ModelRouter(s)
    assert router.pro() is router.pro()
    # Streaming is a different client, not the same one reconfigured.
    assert router.pro(streaming=True) is not router.pro()
    assert router.pro(streaming=True) is router.pro(streaming=True)


def test_for_role_matches_the_named_roles(settings) -> None:
    s = replace(settings, api_key="sk-test")
    router = ModelRouter(s)
    assert router.for_role("pro") is router.pro()
    assert router.for_role("flash") is router.flash()


def test_flash_falls_back_to_the_main_model(settings) -> None:
    router = ModelRouter(replace(settings, model="deepseek-chat", flash_model=None))
    assert router.model_name("flash") == "deepseek-chat"
