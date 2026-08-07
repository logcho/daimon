"""web_search parsing — canned DuckDuckGo HTML, no network. The provider
round-trip against the live network is deliberately not tested here."""

from __future__ import annotations

from daimon_agent.tools.search import parse_ddg_html, resolve_result_url

_RESULT = """
<div class="result">
  <div class="links_main"><a class="result__a" href="{href}">Pizza Dough Recipe</a></div>
  <a class="result__snippet">A foolproof overnight pizza dough.</a>
</div>
"""


def test_resolve_result_url_unwraps_uddg() -> None:
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Frecipe&rut=abc"
    assert resolve_result_url(wrapped) == "https://example.com/recipe"
    assert resolve_result_url("https://example.com/direct") == "https://example.com/direct"


def test_parse_ddg_html_extracts_results() -> None:
    html = _RESULT.format(href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpizza")
    results = parse_ddg_html(html)
    assert len(results) == 1
    assert results[0].title == "Pizza Dough Recipe"
    assert results[0].url == "https://example.com/pizza"
    assert results[0].snippet == "A foolproof overnight pizza dough."


def test_parse_ddg_html_caps_at_eight() -> None:
    html = "".join(_RESULT.format(href=f"https://example.com/{i}") for i in range(12))
    results = parse_ddg_html(html)
    assert len(results) == 8


def test_parse_ddg_html_skips_non_results() -> None:
    html = "<div class='result'><span>no link here</span></div>"
    assert parse_ddg_html(html) == []
