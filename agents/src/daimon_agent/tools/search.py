"""Web search — DuckDuckGo's HTML endpoint via httpx + BeautifulSoup, with
Tavily as an opt-in fallback when TAVILY_API_KEY is set.

Both providers return the same stable `list[SearchResult]` shape so they're
swappable. The DDG parser mirrors the legacy extraction (result blocks,
`uddg` redirect unwrapping, 8-result cap) but on plain HTML instead of the
PinchTab snapshot — one less dependency on the browser for the most
loop-prone tool.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

DDG_URL = "https://html.duckduckgo.com/html/"

# A small pool of recent Chrome user agents — rotated per request so
# repeated searches don't look identical and trigger rate-limiting.
_CHROME_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
]

# Retry config for DuckDuckGo's rate-limiting (403/429).
_SEARCH_MAX_RETRIES = 3
_SEARCH_RETRY_BACKOFF = (1.0, 2.0, 4.0)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def resolve_result_url(href: str, base: str = DDG_URL) -> str:
    """Unwrap DuckDuckGo's `//duckduckgo.com/l/?uddg=<encoded>` redirect so the
    agent gets a URL it can open directly."""
    try:
        absolute = href if href.startswith("http") else urljoin(base, href)
        if absolute.startswith("//"):
            absolute = f"https:{absolute}"
        uddg = parse_qs(urlparse(absolute).query).get("uddg")
        return uddg[0] if uddg else absolute
    except Exception:
        return href


def parse_ddg_html(html: str, base: str = DDG_URL) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[SearchResult] = []
    for block in soup.select(".result"):
        link = block.select_one(".result__a")
        if link is None:
            continue
        title = link.get_text(" ", strip=True)
        snippet_el = block.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        results.append(SearchResult(title=title, url=resolve_result_url(link.get("href", ""), base), snippet=snippet))
        if len(results) >= 8:
            break
    return results


class DuckDuckGoProvider:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, max_results: int = 8) -> list[SearchResult]:
        last_error: str | None = None
        for attempt in range(_SEARCH_MAX_RETRIES):
            ua = random.choice(_CHROME_USER_AGENTS)
            headers = {
                "User-Agent": ua,
                "Accept-Language": "en-US,en;q=0.9",
            }
            try:
                res = await self._client.get(
                    DDG_URL, params={"q": query}, headers=headers
                )
                if res.status_code in (403, 429):
                    last_error = (
                        f"DuckDuckGo returned {res.status_code}"
                    )
                    if attempt < _SEARCH_MAX_RETRIES - 1:
                        delay = _SEARCH_RETRY_BACKOFF[attempt]
                        await asyncio.sleep(delay)
                        continue
                    raise httpx.HTTPStatusError(
                        f"rate-limited after {_SEARCH_MAX_RETRIES} attempts",
                        request=res.request,
                        response=res,
                    )
                res.raise_for_status()
                return parse_ddg_html(res.text)[:max_results]
            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt < _SEARCH_MAX_RETRIES - 1:
                    delay = _SEARCH_RETRY_BACKOFF[attempt]
                    await asyncio.sleep(delay)
                    continue
                raise

        # Should not be reached (the loop either returns or raises),
        # but keep as a safety net.
        raise httpx.HTTPError(last_error or "search failed")


class TavilyProvider:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str) -> list[SearchResult]:
        res = await self._client.post(
            "https://api.tavily.com/search",
            json={"api_key": self._api_key, "query": query, "max_results": 8, "search_depth": "basic"},
        )
        res.raise_for_status()
        data = res.json()
        return [
            SearchResult(title=str(r.get("title", "")), url=str(r.get("url", "")), snippet=str(r.get("content", "")))
            for r in data.get("results", [])
        ]


# ---------------------------------------------------------------------------
# Process-wide singleton (mirrors browser.py's build_browser / aclose_browser)
# ---------------------------------------------------------------------------

_provider: DuckDuckGoProvider | TavilyProvider | None = None


def build_search_provider(settings: Any):
    """Tavily when configured, else DuckDuckGo — same interface either way."""
    global _provider
    api_key = getattr(settings, "tavily_api_key", None)
    _provider = TavilyProvider(api_key) if api_key else DuckDuckGoProvider()
    return _provider


async def aclose_search_provider() -> None:
    global _provider
    if _provider is not None:
        await _provider.aclose()
        _provider = None
