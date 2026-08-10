"""What each provider offers, for the model pickers.

Stubbed httpx — CI has no network. The response shapes are what the live APIs
actually return (probed while writing this): both answer `{"data": [{"id": …}]}`,
DeepSeek at `/models` with a Bearer token, Anthropic at `/v1/models` with
`x-api-key` and an `anthropic-version` header.
"""

from __future__ import annotations

from dataclasses import replace

from daimon_agent.providers import MODEL_CATALOG, list_models, provider_key


class _Response:
    def __init__(self, payload, status: int = 200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None, status: int = 200, boom: bool = False):
        self.payload = payload
        self.status = status
        self.boom = boom
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        if self.boom:
            raise RuntimeError("network down")
        return _Response(self.payload, self.status)

    async def aclose(self):
        pass


DEEPSEEK_LIVE = {"data": [{"id": "deepseek-v4-flash"}, {"id": "deepseek-v4-pro"}]}
ANTHROPIC_LIVE = {"data": [{"id": "claude-sonnet-5"}, {"id": "claude-opus-5"}]}


# --- live fetch --------------------------------------------------------------

async def test_deepseek_is_asked_with_a_bearer_token(settings) -> None:
    cfg = replace(settings, api_key="sk-test", model="deepseek-chat")
    client = _FakeClient(DEEPSEEK_LIVE)
    models, source = await list_models("deepseek", cfg, client=client)

    url, headers = client.calls[0]
    assert url.endswith("/models")
    assert headers["Authorization"] == "Bearer sk-test"
    assert source == "live"
    assert "deepseek-v4-pro" in models


async def test_anthropic_is_asked_with_its_own_headers(settings) -> None:
    cfg = replace(settings, anthropic_api_key="sk-ant-test")
    client = _FakeClient(ANTHROPIC_LIVE)
    models, source = await list_models("anthropic", cfg, client=client)

    url, headers = client.calls[0]
    assert url.endswith("/v1/models")
    assert headers["x-api-key"] == "sk-ant-test"
    assert headers["anthropic-version"]  # required, or the API refuses
    assert source == "live"
    assert "claude-sonnet-5" in models


async def test_api_base_is_honoured(settings) -> None:
    cfg = replace(settings, api_key="sk-test", api_base="https://proxy.example/v1")
    client = _FakeClient(DEEPSEEK_LIVE)
    await list_models("deepseek", cfg, client=client)
    assert client.calls[0][0] == "https://proxy.example/v1/models"


# --- the fallbacks -----------------------------------------------------------

async def test_no_key_falls_back_to_the_catalogue(settings) -> None:
    """A picker still needs options before you've pasted a key — that's the
    moment you most need to see what's on offer."""
    cfg = replace(settings, api_key=None, anthropic_api_key=None)
    client = _FakeClient(DEEPSEEK_LIVE)
    models, source = await list_models("anthropic", cfg, client=client)

    assert source == "catalog"
    assert client.calls == []  # nothing to authenticate with, so nothing asked
    assert set(MODEL_CATALOG["anthropic"]).issubset(set(models))


async def test_a_network_failure_degrades_to_the_catalogue(settings) -> None:
    cfg = replace(settings, api_key="sk-test")
    models, source = await list_models("deepseek", cfg, client=_FakeClient(boom=True))
    assert source == "catalog"
    assert models


async def test_an_error_status_degrades_to_the_catalogue(settings) -> None:
    cfg = replace(settings, api_key="sk-test")
    models, source = await list_models(
        "deepseek", cfg, client=_FakeClient({"error": "nope"}, status=401)
    )
    assert source == "catalog"
    assert models


# --- merging -----------------------------------------------------------------

async def test_aliases_survive_the_live_list(settings) -> None:
    """DeepSeek's /models returns only concrete names, so a picker built from
    it alone would omit `deepseek-chat` — the value most installs are set to."""
    cfg = replace(settings, api_key="sk-test", model="deepseek-chat")
    models, _ = await list_models("deepseek", cfg, client=_FakeClient(DEEPSEEK_LIVE))
    assert "deepseek-chat" in models
    assert "deepseek-reasoner" in models
    assert "deepseek-v4-flash" in models


async def test_the_configured_model_is_always_selectable(settings) -> None:
    """Otherwise opening the picker would silently change what's set."""
    cfg = replace(
        settings, api_key="sk-test", model="deepseek-some-snapshot-20260101",
        flash_model="deepseek-another",
    )
    models, _ = await list_models("deepseek", cfg, client=_FakeClient(DEEPSEEK_LIVE))
    assert "deepseek-some-snapshot-20260101" in models
    assert "deepseek-another" in models


async def test_a_models_other_provider_is_not_mixed_in(settings) -> None:
    cfg = replace(
        settings, api_key="sk-test", model="anthropic:claude-sonnet-5",
        flash_model="deepseek-chat",
    )
    models, _ = await list_models("deepseek", cfg, client=_FakeClient(DEEPSEEK_LIVE))
    assert "claude-sonnet-5" not in models


def test_provider_key_reads_settings_and_environment(settings, monkeypatch) -> None:
    cfg = replace(settings, api_key=None, anthropic_api_key=None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert provider_key("deepseek", cfg) is None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    assert provider_key("deepseek", cfg) == "from-env"
    assert provider_key("deepseek", replace(cfg, api_key="from-settings")) == "from-settings"
