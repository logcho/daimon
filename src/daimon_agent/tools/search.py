"""Web search — DuckDuckGo's HTML endpoint via httpx + BeautifulSoup, with
Tavily as an opt-in fallback when TAVILY_API_KEY is set.

Both providers return the same stable `list[SearchResult]` shape so they're
swappable. The DDG parser mirrors the legacy extraction (result blocks,
`uddg` redirect unwrapping, 8-result cap) but on plain HTML instead of the
PinchTab snapshot — one less dependency on the browser for the most
loop-prone tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

DDG_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


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

    async def search(self, query: str) -> list[SearchResult]:
        res = await self._client.get(DDG_URL, params={"q": query}, headers={"User-Agent": USER_AGENT})
        res.raise_for_status()
        return parse_ddg_html(res.text)


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


def build_search_provider(settings: Any):
    """Tavily when configured, else DuckDuckGo — same interface either way."""
    api_key = getattr(settings, "tavily_api_key", None)
    return TavilyProvider(api_key) if api_key else DuckDuckGoProvider()
