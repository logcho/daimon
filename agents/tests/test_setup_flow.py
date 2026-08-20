"""The `/setup` wizard as a sequence.

The flow is deliberately free of prompt_toolkit, so the whole thing can be
driven by calling `start()` and `answer()` in order. What's checked here is the
behaviour that matters: it writes what you chose, it skips what you've already
got, and it never leaves you configured halfway.
"""

from __future__ import annotations

import pytest

from daimon_agent.cli.setup import PREFIX, SetupFlow, needs_setup


class _FakeServer:
    """Stands in for client.get_config / list_models / update_config, recording
    what the flow tried to write."""

    def __init__(self, *, deepseek_key=False, anthropic_key=False, anthropic_installed=True):
        self.written: list[dict] = []
        self.config = {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "flash_model": "deepseek-chat",
            "providers": {
                "deepseek": {"installed": True, "key_configured": deepseek_key},
                "anthropic": {
                    "installed": anthropic_installed,
                    "key_configured": anthropic_key,
                },
            },
        }
        self.models = {
            "deepseek": {
                "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat"],
                "source": "live", "installed": True, "key_configured": deepseek_key,
            },
            "anthropic": {
                "models": ["claude-sonnet-5", "claude-haiku-4-5"],
                "source": "live", "installed": anthropic_installed,
                "key_configured": anthropic_key,
            },
        }

    def install(self, monkeypatch) -> None:
        import daimon_agent.cli.setup as mod

        async def get_config(http, port):
            return self.config

        async def list_models(http, port):
            return self.models

        async def update_config(http, port, fields):
            self.written.append(fields)
            # Mirror the server: a key marks that provider configured.
            if "key" in fields:
                which = "anthropic" if fields["key"].startswith("sk-ant-") else "deepseek"
                self.config["providers"][which]["key_configured"] = True
                self.models[which]["key_configured"] = True
            self.config.update({k: v for k, v in fields.items() if k != "key"})
            return {"ok": True}

        monkeypatch.setattr(mod.client, "get_config", get_config)
        monkeypatch.setattr(mod.client, "list_models", list_models)
        monkeypatch.setattr(mod.client, "update_config", update_config)


def _step(ask: dict) -> str:
    return ask["id"][len(PREFIX):]


@pytest.fixture
def server(monkeypatch):
    def make(**kwargs):
        s = _FakeServer(**kwargs)
        s.install(monkeypatch)
        return s

    return make


# --- the happy path ----------------------------------------------------------

async def test_full_flow_from_nothing(server) -> None:
    fake = server()
    flow = SetupFlow()

    _, ask = await flow.start()
    assert _step(ask) == "provider"
    assert [o["label"] for o in ask["options"]] == ["deepseek", "anthropic"]

    _, ask = await flow.answer("provider", "deepseek")
    assert _step(ask) == "key"
    assert ask["secret"] is True  # a key shouldn't sit on screen

    _, ask = await flow.answer("key", "sk-abc123")
    assert _step(ask) == "main_model"
    assert "deepseek-v4-pro" in [o["label"] for o in ask["options"]]

    _, ask = await flow.answer("main_model", "deepseek-v4-pro")
    assert _step(ask) == "flash_model"
    assert ask["options"][0]["label"] == "same as main"  # the common case, first

    lines, ask = await flow.answer("flash_model", "deepseek-v4-flash")
    assert ask is None and flow.done
    assert any("ready" in line for line in lines)

    assert fake.written == [
        {"key": "sk-abc123"},
        {"model": "deepseek-v4-pro"},
        {"flash_model": "deepseek-v4-flash"},
    ]


async def test_an_existing_key_skips_the_key_step(server) -> None:
    fake = server(deepseek_key=True)
    flow = SetupFlow()
    await flow.start()
    lines, ask = await flow.answer("provider", "deepseek")

    assert _step(ask) == "main_model"
    assert any("already has a key" in line for line in lines)
    assert fake.written == []  # nothing written just for walking through


async def test_same_as_main_reuses_the_main_model(server) -> None:
    server(deepseek_key=True)
    flow = SetupFlow()
    await flow.start()
    await flow.answer("provider", "deepseek")
    await flow.answer("main_model", "deepseek-v4-pro")
    await flow.answer("flash_model", "same as main")
    assert flow.main_model == "deepseek-v4-pro"


async def test_a_non_default_provider_gets_a_qualified_spec(server) -> None:
    """The router needs to know where to send it, so anything but the default
    provider is stored as `provider:model`."""
    fake = server(anthropic_key=True)
    flow = SetupFlow()
    await flow.start()
    await flow.answer("provider", "anthropic")
    await flow.answer("main_model", "claude-sonnet-5")
    assert fake.written[-1] == {"model": "anthropic:claude-sonnet-5"}


# --- the ways it stops -------------------------------------------------------

async def test_an_uninstalled_provider_stops_with_the_fix(server) -> None:
    """The fix is a package install, not anything inside the app — say so here
    rather than letting the first model call fail."""
    fake = server(anthropic_installed=False)
    flow = SetupFlow()
    await flow.start()
    lines, ask = await flow.answer("provider", "anthropic")

    assert ask is None
    assert any("uv sync --extra anthropic" in line for line in lines)
    assert fake.written == []


async def test_an_empty_key_cancels_without_writing(server) -> None:
    fake = server()
    flow = SetupFlow()
    await flow.start()
    await flow.answer("provider", "deepseek")
    lines, ask = await flow.answer("key", "   ")

    assert ask is None
    assert any("cancelled" in line for line in lines)
    assert fake.written == []


async def test_a_rejected_write_stops_and_shows_why(server, monkeypatch) -> None:
    """The server's rejections ("no anthropic API key") are the useful part."""
    import daimon_agent.cli.setup as mod

    fake = server(deepseek_key=True)

    async def refuse(http, port, fields):
        return {"error": "can't use x: no deepseek API key is set"}

    monkeypatch.setattr(mod.client, "update_config", refuse)
    flow = SetupFlow()
    await flow.start()
    await flow.answer("provider", "deepseek")
    lines, ask = await flow.answer("main_model", "deepseek-v4-pro")

    assert ask is None
    assert any("no deepseek API key" in line for line in lines)


async def test_an_unreachable_server_stops_at_the_start(server, monkeypatch) -> None:
    import daimon_agent.cli.setup as mod

    async def broken(http, port):
        return {"error": "could not reach the daimon server"}

    monkeypatch.setattr(mod.client, "get_config", broken)
    lines, ask = await SetupFlow().start()
    assert ask is None
    assert any("could not reach" in line for line in lines)


async def test_an_unknown_step_is_ignored(server) -> None:
    server()
    assert await SetupFlow().answer("nonsense", "x") == ([], None)


# --- first launch ------------------------------------------------------------

def test_needs_setup_when_no_provider_has_a_key() -> None:
    assert needs_setup({"providers": {
        "deepseek": {"key_configured": False},
        "anthropic": {"key_configured": False},
    }}) is True


def test_needs_setup_is_false_once_any_provider_is_keyed() -> None:
    assert needs_setup({"providers": {
        "deepseek": {"key_configured": True},
        "anthropic": {"key_configured": False},
    }}) is False


def test_needs_setup_falls_back_to_the_legacy_flag() -> None:
    """An older server without the providers block still answers usefully."""
    assert needs_setup({"api_key_configured": True}) is False
    assert needs_setup({}) is True
