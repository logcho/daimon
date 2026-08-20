"""Tests for the self-contained web_fetch tool (no PinchTab, no network)."""

from __future__ import annotations

import pytest
from daimon_agent.tools.web import WebFetcher, _extract_markdown, MAX_FETCH_CHARS

# ---------------------------------------------------------------------------
# _extract_markdown
# ---------------------------------------------------------------------------

SIMPLE_HTML = """<!DOCTYPE html>
<html><head><title>Test Page</title></head>
<body>
  <article>
    <h1>Hello World</h1>
    <p>This is a test page with some <strong>important</strong> content.</p>
    <p>Another paragraph with <a href="https://example.com">a link</a>.</p>
  </article>
  <nav>Navigation — should be stripped</nav>
  <footer>Footer — should be stripped</footer>
</body></html>"""

NO_ARTICLE_HTML = """<!DOCTYPE html>
<html><head><title>Plain</title></head>
<body>
  <div>Just some text in a div — no semantic containers.</div>
  <script>console.log('should be gone');</script>
  <style>body { color: red; }</style>
</body></html>"""

EMPTY_HTML = "<html><head></head><body><script>foo</script><style>bar</style></body></html>"


def test_extract_markdown_with_article():
    """Trafilatura extracts the article content (or bs4 fallback gets the text)."""
    result = _extract_markdown(SIMPLE_HTML)
    assert result
    assert "Hello World" in result
    assert "important" in result
    # The article content should be the main output
    assert "test page" in result.lower() or "Another paragraph" in result


def test_extract_markdown_no_article():
    """When there's no semantic container, the bs4 fallback gets the body text."""
    result = _extract_markdown(NO_ARTICLE_HTML)
    assert result
    assert "Just some text" in result
    # script/style contents should be gone
    assert "console.log" not in result
    assert "color: red" not in result


def test_extract_markdown_empty():
    """Empty pages produce an empty string."""
    result = _extract_markdown(EMPTY_HTML)
    # Both trafilatura and bs4 should produce empty or near-empty output
    assert result == "" or not result.strip()


# ---------------------------------------------------------------------------
# WebFetcher (offline — httpx.MockTransport)
# ---------------------------------------------------------------------------

import httpx


def _mock_client(mock_transport) -> WebFetcher:
    return WebFetcher(client=httpx.AsyncClient(transport=mock_transport))


@pytest.mark.asyncio
async def test_fetch_html_to_markdown():
    """A successful HTML fetch returns extracted Markdown."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=SIMPLE_HTML,
        )
    )
    fetcher = _mock_client(transport)
    result = await fetcher.fetch("https://example.com/article")
    assert "Hello World" in result
    assert "https://example.com" not in result  # extraction shouldn't return raw HTML


@pytest.mark.asyncio
async def test_fetch_http_error():
    """Non-2xx responses return a friendly error string, never raise."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(403, text="Forbidden")
    )
    fetcher = _mock_client(transport)
    result = await fetcher.fetch("https://blocked.example.com")
    assert "403" in result
    assert "block" in result.lower() or "automated" in result.lower()


@pytest.mark.asyncio
async def test_fetch_pdf_content_type():
    """PDF content-type returns a helpful message, not garbled binary."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            text="%PDF-1.4 fake pdf content",
        )
    )
    fetcher = _mock_client(transport)
    result = await fetcher.fetch("https://example.com/doc.pdf")
    assert "PDF" in result
    assert "not readable" in result.lower() or "content cannot be read" in result.lower()


@pytest.mark.asyncio
async def test_fetch_truncation():
    """Content exceeding MAX_FETCH_CHARS gets truncated with a marker."""
    long_text = "x" * (MAX_FETCH_CHARS + 100)
    html = f"<html><body><p>{long_text}</p></body></html>"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=html,
        )
    )
    fetcher = _mock_client(transport)
    result = await fetcher.fetch("https://example.com/long")
    assert len(result) <= MAX_FETCH_CHARS + 200  # allow room for truncation marker
    assert "truncated" in result.lower()


@pytest.mark.asyncio
async def test_fetch_plain_text():
    """Plain text is returned as-is (not passed through extractors)."""
    text = "Just plain text content.\nLine two."
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text=text,
        )
    )
    fetcher = _mock_client(transport)
    result = await fetcher.fetch("https://example.com/readme.txt")
    assert "Just plain text" in result
    assert "Line two" in result


@pytest.mark.asyncio
async def test_fetch_json_truncated():
    """JSON is returned as raw text (truncated if needed)."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            text='{"key": "value"}',
        )
    )
    fetcher = _mock_client(transport)
    result = await fetcher.fetch("https://api.example.com/data")
    assert '"key": "value"' in result


@pytest.mark.asyncio
async def test_fetch_no_extractable_content():
    """When the page has no extractable text, a helpful message is returned."""
    empty_html = "<html><head></head><body></body></html>"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=empty_html,
        )
    )
    fetcher = _mock_client(transport)
    result = await fetcher.fetch("https://example.com/blank")
    assert "no extractable" in result.lower() or result == ""


async def _cleanup_fetcher(fetcher: WebFetcher) -> None:
    await fetcher.aclose()
