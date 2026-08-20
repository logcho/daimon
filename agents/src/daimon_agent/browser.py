"""The background browser — PinchTab client, port of `legacy/agents/src/browser.ts`.

PinchTab (github.com/pinchtab/pinchtab) drives a real, stealth-patched Chrome
over a local HTTP API — the same layer daimon has always used for its
background browser, kept out of the user's screen per the non-disruption
invariants. Every call goes through a 20s timeout, the content/search tabs are
cached per session, and a 404 ("tab not found" — the signature of a PinchTab
process recycle) drops the stale cache and retries exactly once.

The content tab and the search tab are deliberately separate, so web_search
never disturbs whatever read_page/click/fill are looking at (the era-1 lesson:
a shared tab made read_page return the DuckDuckGo results page and reliably
drove a web_search -> read_page -> open_url loop).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

PINCHTAB_REQUEST_TIMEOUT_S = 20.0


class PinchTabError(Exception):
    pass


class PinchTabHttpError(PinchTabError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _is_tab_not_found(err: BaseException) -> bool:
    return isinstance(err, PinchTabHttpError) and err.status == 404


@dataclass
class PageElement:
    ref: str
    role: str
    label: str


@dataclass
class PageContent:
    url: str
    text: str
    elements: list[PageElement]


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class PinchTabClient:
    """Stateless-over-HTTP client with per-session tab caches and self-heal."""

    def __init__(self, base: str, token: str | None = None) -> None:
        self._base = base.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(timeout=PINCHTAB_REQUEST_TIMEOUT_S)
        self._content_tab: str | None = None
        self._search_tab: str | None = None
        self._tab_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- low-level ---------------------------------------------------------

    async def _fetch(self, method: str, path: str, body: Any = None) -> Any:
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            res = await self._client.request(method, f"{self._base}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise PinchTabError(f"PinchTab {method} {path} failed: {exc}") from exc
        if res.status_code >= 400:
            message = ""
            try:
                data = res.json()
                message = str(data.get("error", res.text))
            except Exception:
                message = res.text
            raise PinchTabHttpError(
                f"PinchTab {method} {path} failed ({res.status_code}): {message}",
                res.status_code,
            )
        if not res.content:
            return {}
        try:
            return res.json()
        except Exception:
            return {}

    # -- tab lifecycle -----------------------------------------------------

    async def _open_blank_tab(self) -> str:
        res = await self._fetch("POST", "/navigate", {"url": "about:blank", "newTab": True})
        tab_id = res.get("tabId")
        if not tab_id:
            raise PinchTabError("PinchTab /navigate returned no tabId")
        return str(tab_id)

    async def _ensure_content_tab(self) -> str:
        async with self._tab_lock:
            if self._content_tab is None:
                self._content_tab = await self._open_blank_tab()
            return self._content_tab

    async def _ensure_search_tab(self) -> str:
        async with self._tab_lock:
            if self._search_tab is None:
                self._search_tab = await self._open_blank_tab()
            return self._search_tab

    async def new_tab(self, url: str = "about:blank") -> str:
        """Open a fresh tab and make it the content tab."""
        res = await self._fetch("POST", "/navigate", {"url": url, "newTab": True})
        tab_id = str(res.get("tabId", ""))
        if not tab_id:
            raise PinchTabError("PinchTab /navigate returned no tabId")
        self._content_tab = tab_id
        return tab_id

    async def switch_tab(self, tab_id: str) -> None:
        """Point the content tab at an existing tab id (from list_tabs)."""
        self._content_tab = tab_id

    async def _with_tab(self, ensure_tab, cache_attr: str, op) -> Any:
        """Run op(tab_id); on a 404 (recycle signature) drop the cached tab
        and retry exactly once against a fresh one."""
        tab_id = await ensure_tab()
        try:
            return await op(str(tab_id))
        except PinchTabHttpError as err:
            if not _is_tab_not_found(err):
                raise
            setattr(self, cache_attr, None)
            fresh = await ensure_tab()
            return await op(str(fresh))

    async def _with_content_tab(self, op) -> Any:
        return await self._with_tab(self._ensure_content_tab, "_content_tab", op)

    async def _with_search_tab(self, op) -> Any:
        return await self._with_tab(self._ensure_search_tab, "_search_tab", op)

    # -- actions -----------------------------------------------------------

    async def open_url(self, url: str) -> str:
        async def op(tab_id: str) -> str:
            res = await self._fetch("POST", f"/tabs/{tab_id}/navigate", {"url": url})
            return str(res.get("title", ""))
        return await self._with_content_tab(op)

    async def read_page(self) -> PageContent:
        async def op(tab_id: str) -> PageContent:
            text_res, snap_res = await asyncio.gather(
                # mode=raw for document.body.innerText (matches legacy exactly
                # — Readability extraction would drop form fields/buttons the
                # agent still needs to see). maxChars mirrors legacy's own
                # truncation, done server-side.
                self._fetch("GET", f"/tabs/{tab_id}/text?mode=raw&maxChars=4000"),
                # maxTokens bounds the response on element-heavy pages.
                self._fetch("GET", f"/tabs/{tab_id}/snapshot?filter=interactive&maxTokens=2000"),
            )
            elements = [
                PageElement(ref=str(n.get("ref", "")), role=str(n.get("role", "")), label=str(n.get("name", "")))
                for n in (snap_res.get("nodes") or [])
            ]
            return PageContent(
                url=str(text_res.get("url", "")),
                text=str(text_res.get("text", "")),
                elements=elements,
            )
        return await self._with_content_tab(op)

    async def extract_text(self) -> str:
        """Readability-style extraction of the main article content (PinchTab's
        default when mode is omitted) — for reading long-form pages."""
        async def op(tab_id: str) -> str:
            res = await self._fetch("GET", f"/tabs/{tab_id}/text?maxChars=8000")
            return str(res.get("text", ""))
        return await self._with_content_tab(op)

    async def click(self, ref: str) -> None:
        async def op(tab_id: str) -> None:
            # waitNav: true — legacy proved empirically that navigating actions
            # flake without it (500 "unexpected page navigation") while
            # non-navigating actions return immediately anyway; no downside.
            await self._fetch("POST", f"/tabs/{tab_id}/action", {"kind": "click", "ref": ref, "waitNav": True})
        await self._with_content_tab(op)

    async def fill(self, ref: str, text: str) -> None:
        async def op(tab_id: str) -> None:
            await self._fetch(
                "POST", f"/tabs/{tab_id}/action", {"kind": "fill", "ref": ref, "text": text, "waitNav": True}
            )
        await self._with_content_tab(op)

    async def screenshot_base64(self) -> str:
        async def op(tab_id: str) -> str:
            res = await self._fetch("GET", f"/tabs/{tab_id}/screenshot?format=png")
            return str(res.get("base64", ""))
        return await self._with_content_tab(op)

    async def list_tabs(self) -> list[dict]:
        res = await self._fetch("GET", "/tabs")
        return res if isinstance(res, list) else []

    async def close_tab(self, tab_id: str) -> None:
        await self._fetch("DELETE", f"/tabs/{tab_id}")
        if self._content_tab == tab_id:
            self._content_tab = None
        if self._search_tab == tab_id:
            self._search_tab = None

    # -- search tab --------------------------------------------------------

    async def search(self, query: str) -> list[SearchResult]:
        async def op(tab_id: str) -> list[SearchResult]:
            url = f"https://html.duckduckgo.com/html/?q={query}"
            await self._fetch("POST", f"/tabs/{tab_id}/navigate", {"url": url})
            return await self._extract_search_results(tab_id)
        return await self._with_search_tab(op)

    async def _extract_search_results(self, tab_id: str) -> list[SearchResult]:
        # Legacy proved the a11y snapshot carries no hrefs, so each title
        # link's href is read via the ungated /attr endpoint, then the uddg
        # redirect wrapper is unwrapped.
        import urllib.parse

        snap = await self._fetch(
            "GET", f"/tabs/{tab_id}/snapshot?filter=interactive&selector={urllib.parse.quote('#links')}"
        )
        nodes = snap.get("nodes") or []
        results: list[SearchResult] = []

        def resolve_result_url(href: str) -> str:
            try:
                absolute = f"https:{href}" if href.startswith("//") else href
                parsed = urllib.parse.urlparse(absolute)
                wrapped = urllib.parse.parse_qs(parsed.query).get("uddg", [None])[0]
                return urllib.parse.unquote(wrapped) if wrapped else absolute
            except Exception:
                return href

        i = 0
        while i < len(nodes) and len(results) < 8:
            node = nodes[i]
            if node.get("role") != "heading":
                i += 1
                continue
            title_link = nodes[i + 1] if i + 1 < len(nodes) else None
            if not title_link or title_link.get("role") != "link" or not title_link.get("name"):
                i += 1
                continue
            snippet = ""
            j = i + 2
            while j < len(nodes) and nodes[j].get("role") != "heading":
                if nodes[j].get("role") == "link" and nodes[j].get("name"):
                    snippet = nodes[j]["name"]
                j += 1
            url = ""
            try:
                attr = await self._fetch(
                    "GET",
                    f"/tabs/{tab_id}/attr?selector={urllib.parse.quote(title_link['ref'])}&name=href",
                )
                if attr.get("value"):
                    url = resolve_result_url(str(attr["value"]))
            except PinchTabError:
                pass  # title/snippet still useful without the URL
            results.append(SearchResult(title=title_link["name"], url=url, snippet=snippet))
            i = j
        return results


_browser: PinchTabClient | None = None


def build_browser(settings: Any) -> PinchTabClient | None:
    """The process's one browser client — shared by the tools, the live-frame
    poller, and server shutdown, because the agent process *is* one workspace
    (the legacy daemon spawned one process per session). None when PinchTab
    isn't configured, so tools report it as unavailable instead of failing at
    build time."""
    global _browser
    if _browser is None and getattr(settings, "pinchtab_base", None):
        _browser = PinchTabClient(settings.pinchtab_base, getattr(settings, "pinchtab_token", None))
    return _browser


async def aclose_browser() -> None:
    global _browser
    if _browser is not None:
        await _browser.aclose()
        _browser = None
