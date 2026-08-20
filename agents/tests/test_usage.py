"""Token and cost accounting: extraction across provider shapes, the cache
discount, and the rule that an unpriced model produces no cost at all rather
than a plausible-looking zero."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from daimon_agent.usage import (
    CallUsage,
    UsageAccumulator,
    extract_usage,
    format_tokens,
    price_for,
    record,
    set_active_usage,
)


# --- pricing -----------------------------------------------------------------

def test_price_lookup_matches_dated_snapshots() -> None:
    """A snapshot suffix must not fall off the price list — otherwise every
    pinned model silently reports no cost."""
    assert price_for("claude-sonnet-5-20260101") is price_for("claude-sonnet-5")
    assert price_for("deepseek-chat") is not None
    assert price_for("some-local-model") is None


def test_cost_bills_cache_reads_at_the_cache_rate() -> None:
    """Cache reads are a subset of input tokens, so the uncached remainder
    bills at the full rate and the rest at the discount."""
    price = price_for("deepseek-chat")
    usage = CallUsage(
        model="deepseek-chat",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=900_000,
    )
    expected = (100_000 * price.input + 900_000 * price.cache_read) / 1_000_000
    assert usage.cost_usd() == expected
    # And the discount is real: an all-cached call costs far less.
    assert usage.cost_usd() < CallUsage(
        model="deepseek-chat", input_tokens=1_000_000, output_tokens=0
    ).cost_usd()


def test_unpriced_model_has_no_cost() -> None:
    assert CallUsage(model="mystery", input_tokens=10, output_tokens=5).cost_usd() is None


# --- extraction --------------------------------------------------------------

def test_extract_prefers_normalized_usage_metadata() -> None:
    message = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 80},
        },
        response_metadata={"model_name": "deepseek-chat"},
    )
    usage = extract_usage(message)
    assert usage.model == "deepseek-chat"
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (100, 20, 80)


def test_extract_falls_back_to_deepseeks_raw_cache_field() -> None:
    """DeepSeek reports cache hits as `prompt_cache_hit_tokens`, which isn't
    part of the normalized shape."""
    message = AIMessage(
        content="hi",
        response_metadata={
            "model_name": "deepseek-chat",
            "token_usage": {
                "prompt_tokens": 200,
                "completion_tokens": 30,
                "prompt_cache_hit_tokens": 150,
            },
        },
    )
    usage = extract_usage(message)
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (200, 30, 150)


def test_billing_falls_back_to_the_requested_alias() -> None:
    """Providers resolve aliases: ask for `deepseek-chat` and the response
    says `deepseek-v4-flash`. You're billed at the alias's rate, so pricing
    has to follow the alias rather than needing an entry per resolved name."""
    usage = CallUsage(
        model="deepseek-v4-flash",
        billing_model="deepseek-chat",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert usage.model == "deepseek-v4-flash"  # display keeps the real name
    assert usage.cost_usd() == price_for("deepseek-chat").input


def test_extract_keeps_both_the_served_and_requested_names() -> None:
    message = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        response_metadata={"model_name": "deepseek-v4-flash"},
    )
    usage = extract_usage(message, fallback_model="deepseek-chat")
    assert usage.model == "deepseek-v4-flash"
    assert usage.billing_model == "deepseek-chat"
    assert usage.cost_usd() is not None


def test_price_overrides_come_from_the_environment(monkeypatch) -> None:
    """Provider prices change; this is the escape hatch that doesn't need a
    release."""
    import importlib

    monkeypatch.setenv("DAIMON_PRICES", '{"my-model": [1.0, 2.0, 0.1]}')
    import daimon_agent.usage as usage_mod

    importlib.reload(usage_mod)
    try:
        assert usage_mod.price_for("my-model").output == 2.0
    finally:
        monkeypatch.delenv("DAIMON_PRICES", raising=False)
        importlib.reload(usage_mod)
    assert usage_mod.price_for("my-model") is None


def test_a_malformed_price_override_is_ignored() -> None:
    """It costs cost-reporting, not the turn."""
    import importlib
    import os

    os.environ["DAIMON_PRICES"] = "{not json"
    import daimon_agent.usage as usage_mod

    try:
        importlib.reload(usage_mod)  # must not raise
    finally:
        del os.environ["DAIMON_PRICES"]
        importlib.reload(usage_mod)


def test_extract_returns_none_without_usage() -> None:
    """A scripted model, or a stream without stream_usage, reports nothing —
    and nothing is not zero."""
    assert extract_usage(AIMessage(content="hi")) is None
    assert extract_usage(None) is None


# --- the accumulator ---------------------------------------------------------

def test_accumulator_sums_and_splits_by_model() -> None:
    acc = UsageAccumulator()
    acc.add(CallUsage(model="deepseek-chat", input_tokens=100, output_tokens=10))
    acc.add(CallUsage(model="deepseek-chat", input_tokens=50, output_tokens=5))
    acc.add(CallUsage(model="claude-sonnet-5", input_tokens=10, output_tokens=1))

    assert acc.input_tokens == 160
    assert acc.output_tokens == 16
    assert acc.calls == 3
    assert set(acc.by_model) == {"deepseek-chat", "claude-sonnet-5"}
    assert acc.by_model["deepseek-chat"].input_tokens == 150
    assert acc.cost_usd > 0


def test_one_unpriced_call_makes_the_whole_total_unknown() -> None:
    """A partial total reads as a complete one, which is worse than no number."""
    acc = UsageAccumulator()
    acc.add(CallUsage(model="deepseek-chat", input_tokens=100, output_tokens=10))
    assert acc.cost_usd is not None
    acc.add(CallUsage(model="mystery", input_tokens=100, output_tokens=10))
    assert acc.cost_usd is None
    assert "cost_usd" not in acc.totals()


def test_context_tokens_tracks_the_latest_main_call() -> None:
    """Context size is the *current* prompt, not the sum of every prompt."""
    acc = UsageAccumulator()
    acc.add(CallUsage(model="m", input_tokens=100, output_tokens=1), is_context=True)
    acc.add(CallUsage(model="m", input_tokens=900, output_tokens=1), is_context=True)
    acc.add(CallUsage(model="m", input_tokens=5, output_tokens=1))  # a subagent
    assert acc.context_tokens == 900


def test_record_is_a_noop_outside_a_turn() -> None:
    """Instrumenting a call site should never need a guard."""
    set_active_usage(None)
    record(CallUsage(model="m", input_tokens=1, output_tokens=1))  # must not raise


def test_record_reaches_the_active_accumulator() -> None:
    acc = UsageAccumulator()
    set_active_usage(acc)
    try:
        record(CallUsage(model="deepseek-chat", input_tokens=7, output_tokens=3))
    finally:
        set_active_usage(None)
    assert acc.input_tokens == 7


def test_format_tokens_is_compact() -> None:
    assert format_tokens(942) == "942"
    assert format_tokens(12_400) == "12.4k"
    assert format_tokens(1_830_000) == "1.83M"


# --- through the graph -------------------------------------------------------

async def test_usage_events_are_emitted_per_model_call(settings) -> None:
    from daimon_agent.graph import build_graph, run_config
    from langchain_core.messages import HumanMessage

    from fakes import FakeRouter

    priced = AIMessage(
        content="answer",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_token_details": {"cache_read": 0},
        },
        response_metadata={"model_name": "deepseek-chat"},
    )
    router = FakeRouter(pro_script=[priced])
    graph = build_graph(settings, router, [])
    events: list[dict] = []

    from daimon_agent.emitter import set_active_emit

    set_active_emit(events.append)
    try:
        await graph.ainvoke({"messages": [HumanMessage(content="go")]}, run_config("usage"))
    finally:
        set_active_emit(None)

    usage_events = [e for e in events if e["type"] == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["input_tokens"] == 1000
    assert usage_events[0]["role"] == "pro"
    assert usage_events[0]["cost_usd"] > 0


def test_deepseek_v4_models_are_priced() -> None:
    """Both concrete names *and* the aliases, because a model spec may say
    either — and the picker now offers the concrete ones. An unpriced name
    silently renders a blank cost, which reads as "free" rather than
    "unknown"."""
    for name in ("deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"):
        assert price_for(name) is not None, name


def test_cache_hits_are_dramatically_cheaper_on_deepseek() -> None:
    """A 50x gap on flash — the whole reason prompts.py keeps a byte-stable
    frozen prefix. If this ever inverts, the caching work stopped paying."""
    flash = price_for("deepseek-v4-flash")
    assert flash.cache_read * 10 < flash.input


def test_pro_costs_more_than_flash() -> None:
    pro, flash = price_for("deepseek-v4-pro"), price_for("deepseek-v4-flash")
    assert pro.input > flash.input and pro.output > flash.output
