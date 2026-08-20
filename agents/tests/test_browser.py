"""PinchTab client — live-browser tests only. Skipped unless PINCHTAB_BASE is
set (CI never runs them); unit-level URL/tab-cache logic lives in the client
and is exercised here when a browser is available.

Run with a live server:
    PINCHTAB_BASE=http://127.0.0.1:9223 PINCHTAB_TOKEN=... uv run pytest tests/test_browser.py
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.browser

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not os.environ.get("PINCHTAB_BASE"), reason="PINCHTAB_BASE not set"),
]

from daimon_agent.browser import PinchTabClient  # noqa: E402


@pytest.fixture
def client() -> PinchTabClient:
    return PinchTabClient(os.environ["PINCHTAB_BASE"], os.environ.get("PINCHTAB_TOKEN", ""))


@pytest.mark.asyncio
async def test_open_url_and_read_page(client: PinchTabClient) -> None:
    await client.open_url("https://example.com")
    text, snapshot = await client.read_page()
    assert "Example Domain" in text
    assert snapshot  # interactive snapshot is non-empty


@pytest.mark.asyncio
async def test_search_returns_results(client: PinchTabClient) -> None:
    results = await client.search("python web framework")
    assert results
    assert all(r.url.startswith("http") for r in results)
