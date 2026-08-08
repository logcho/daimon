"""Self-contained web fetching — httpx + trafilatura extraction with a
BeautifulSoup fallback. No PinchTab, no external service, no API keys.

``web_fetch`` is the browser-free replacement for the open_url + read_page
loop on static pages. It fetches a URL with Chrome-impersonating headers and
returns the main content as clean Markdown.
"""

from __future__ import annotations

import random
from typing import Any

import httpx
from bs4 import BeautifulSoup

# A small pool of recent Chrome user agents — rotated per request so
# repeated calls to the same host don't look identical.
_CHROME_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
]

_CHROME_HEADERS_TEMPLATE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

MAX_FETCH_CHARS = 12_000
FETCH_TIMEOUT_S = 20.0


def _chrome_headers() -> dict[str, str]:
    """Chrome-impersonating request headers with a random UA from the pool."""
    ua = random.choice(_CHROME_USER_AGENTS)
    return {"User-Agent": ua, **_CHROME_HEADERS_TEMPLATE}


def _extract_markdown(html: str, url: str = "") -> str:
    """Extract the main content from HTML as Markdown.

    Trafilatura first (best-in-class extraction, 0.94 F1, Markdown output);
    BeautifulSoup text fallback when trafilatura produces nothing (JS-heavy
    pages, non-article content).
    """
    try:
        import trafilatura

        result = trafilatura.extract(
            html,
            output_format="markdown",
            url=url,
            include_comments=False,
            include_links=True,
        )
        if result and result.strip():
            return result.strip()
    except Exception:
        pass

    # BeautifulSoup fallback — strip script/style/nav/footer/header, then
    # extract remaining text.
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()

        # Prefer semantic containers if present.
        main = soup.find("main") or soup.find("article") or soup.find("body")
        if main is None:
            main = soup
        text = main.get_text(separator="\n", strip=True)
        return text if text else ""
    except Exception:
        return ""


class WebFetcher:
    """Duck-typed HTTP client for URL fetching and content extraction."""

    def __init__(self, client: Any = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
        )

    async def fetch(self, url: str) -> str:
        """GET *url* and return extracted Markdown/text.

        Never raises — returns a friendly error string on failure so the
        model can adapt instead of crashing the tool node.
        """
        # --- fetch ----------------------------------------------------------
        try:
            resp = await self._client.get(url, headers=_chrome_headers())
        except httpx.InvalidURL as exc:
            return f"Invalid URL: {exc}"
        except httpx.TimeoutException:
            return f"Timed out fetching {url} after {FETCH_TIMEOUT_S}s — the server may be slow or unreachable."
        except httpx.HTTPError as exc:
            return f"Failed to fetch {url}: {exc}"

        if resp.status_code >= 400:
            return (
                f"Server returned {resp.status_code} for {url}. "
                "The page may require authentication, be behind a paywall, "
                "or block automated requests."
            )

        # --- content-type sniff --------------------------------------------
        content_type = resp.headers.get("content-type", "").lower()

        if "text/html" in content_type or "application/xhtml" in content_type:
            text = _extract_markdown(resp.text, url)
            if not text:
                return "(page returned no extractable content)"
        elif "text/plain" in content_type:
            text = resp.text
        elif "application/json" in content_type:
            text = resp.text[:MAX_FETCH_CHARS]
        elif "application/pdf" in content_type:
            return (
                f"{url} is a PDF — content cannot be read as text. "
                "Try web_search for an HTML version, or download and extract locally."
            )
        else:
            # Unknown type — try best-effort text extraction anyway
            text = _extract_markdown(resp.text, url)
            if not text:
                return (
                    f"{url} returned content-type '{content_type}' which is "
                    "not readable as text."
                )

        # --- truncate ------------------------------------------------------
        if len(text) > MAX_FETCH_CHARS:
            text = text[:MAX_FETCH_CHARS] + (
                f"\n\n… (truncated at {MAX_FETCH_CHARS:,} chars — "
                "use open_url + read_page for longer pages)"
            )

        return text

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Process-wide singleton (mirrors browser.py's build_browser / aclose_browser)
# ---------------------------------------------------------------------------

_fetcher: WebFetcher | None = None


def build_web_fetcher(settings: Any) -> WebFetcher:
    """The process's one WebFetcher, shared by the tool handler and server
    shutdown."""
    global _fetcher
    if _fetcher is None:
        _fetcher = WebFetcher()
    return _fetcher


async def aclose_web_fetcher() -> None:
    global _fetcher
    if _fetcher is not None:
        await _fetcher.aclose()
        _fetcher = None
