"""Token and cost accounting.

Every model call in the process — the main agent, subagents, the compaction
summarizer, the reflection pass — reports through here, so "what did that turn
cost" has one answer instead of several partial ones.

The accumulator lives in a ContextVar for the same reason `emitter.py` does:
turns run on separate asyncio tasks, and a task inherits a *copy* of the
context at creation. A subagent spawned with `create_task` therefore writes
into the accumulator its parent installed, while two concurrent turns never
touch each other's totals.

Prices are $ per million tokens. An unpriced model yields `cost_usd = None`
rather than a plausible-looking zero — a UI that can't cost a call should say
so, not lie by omission.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# --- pricing ----------------------------------------------------------------

@dataclass(frozen=True)
class Price:
    """$ per 1M tokens."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


#: Keyed by model name (no provider prefix). Matched longest-prefix-first, so
#: dated snapshots like `claude-sonnet-5-20260101` hit their base entry.
#:
#: These are published list prices at time of writing, and providers change
#: them. `DAIMON_PRICES` overrides any entry without a code change — see
#: `_load_price_overrides`. An unlisted model costs nothing rather than
#: guessing, so a stale table under-reports instead of inventing a number.
PRICES: dict[str, Price] = {
    # DeepSeek, from api-docs.deepseek.com/quick_start/pricing. `input` is the
    # cache-*miss* rate and `cache_read` the hit rate — a 50x difference on
    # flash, which is why prompts.py bothers with a byte-stable frozen prefix.
    "deepseek-v4-flash": Price(input=0.14, output=0.28, cache_read=0.0028),
    "deepseek-v4-pro": Price(input=0.435, output=0.87, cache_read=0.003625),
    # The aliases, priced as whatever they currently resolve to. Listed
    # separately because a model spec may name either, and an unpriced name
    # silently drops cost reporting.
    "deepseek-chat": Price(input=0.14, output=0.28, cache_read=0.0028),
    "deepseek-reasoner": Price(input=0.435, output=0.87, cache_read=0.003625),
    # Anthropic — cache writes cost 1.25x input, reads 0.1x.
    "claude-opus-4": Price(input=15.0, output=75.0, cache_read=1.50, cache_write=18.75),
    "claude-opus-5": Price(input=15.0, output=75.0, cache_read=1.50, cache_write=18.75),
    "claude-sonnet-4": Price(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75),
    "claude-sonnet-5": Price(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75),
    "claude-haiku-4-5": Price(input=1.0, output=5.0, cache_read=0.10, cache_write=1.25),
}


def _load_price_overrides() -> None:
    """`DAIMON_PRICES` — JSON of `{"model": [input, output, cache_read?,
    cache_write?]}` in $/Mtok. An escape hatch for a price change or a model
    this table has never heard of, without waiting on a release."""
    raw = os.environ.get("DAIMON_PRICES")
    if not raw:
        return
    try:
        for model, values in (json.loads(raw) or {}).items():
            PRICES[str(model)] = Price(*[float(v) for v in values][:4])
    except Exception:
        pass  # a malformed override costs cost-reporting, not the turn


_load_price_overrides()


def price_for(model: str) -> Price | None:
    """Longest matching prefix, so `claude-sonnet-5-20260101` finds
    `claude-sonnet-5` without needing every snapshot enumerated."""
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    best: str | None = None
    for name in PRICES:
        if model.startswith(name) and (best is None or len(name) > len(best)):
            best = name
    return PRICES[best] if best else None


# --- one call's usage --------------------------------------------------------

@dataclass
class CallUsage:
    #: What the provider says it served. Often not what was requested:
    #: DeepSeek resolves the `deepseek-chat` alias to a concrete model name
    #: like `deepseek-v4-flash` and reports that.
    model: str = ""
    #: What was asked for. Billing follows the alias when the resolved name
    #: isn't priced — you are charged the alias's rate, and this keeps the
    #: table from needing an entry for every model a provider maps to.
    billing_model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost_usd(self) -> float | None:
        price = price_for(self.model) or price_for(self.billing_model)
        if price is None:
            return None
        # Providers report cache reads as a *subset* of input tokens, so bill
        # the uncached remainder at the full rate and the rest at the cache
        # rate. max() guards a provider that ever reports them separately.
        uncached = max(self.input_tokens - self.cache_read_tokens, 0)
        return (
            uncached * price.input
            + self.cache_read_tokens * price.cache_read
            + self.cache_write_tokens * price.cache_write
            + self.output_tokens * price.output
        ) / 1_000_000


def extract_usage(response: Any, fallback_model: str = "") -> CallUsage | None:
    """Pull usage off a LangChain response.

    Prefers `usage_metadata`, which LangChain normalizes across providers.
    Falls back to the raw `response_metadata["token_usage"]` block, which is
    where DeepSeek's non-standard `prompt_cache_hit_tokens` shows up on clients
    that don't map it. Returns None when a response carries no usage at all
    (a streamed call without `stream_usage`, or a fake model in tests).

    `fallback_model` is the model that was *requested*, kept as the billing
    name — see `CallUsage.billing_model`.
    """
    if response is None:
        return None
    model = (
        getattr(response, "response_metadata", None) or {}
    ).get("model_name") or fallback_model

    meta = getattr(response, "usage_metadata", None)
    if meta:
        details = meta.get("input_token_details") or {}
        return CallUsage(
            model=model,
            billing_model=fallback_model,
            input_tokens=int(meta.get("input_tokens") or 0),
            output_tokens=int(meta.get("output_tokens") or 0),
            cache_read_tokens=int(details.get("cache_read") or 0),
            cache_write_tokens=int(details.get("cache_creation") or 0),
        )

    raw = (getattr(response, "response_metadata", None) or {}).get("token_usage")
    if not raw:
        return None
    details = raw.get("prompt_tokens_details") or {}
    cache_read = (
        raw.get("prompt_cache_hit_tokens")
        or details.get("cached_tokens")
        or 0
    )
    return CallUsage(
        model=model,
        billing_model=fallback_model,
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cache_read_tokens=int(cache_read),
    )


# --- the per-turn accumulator ------------------------------------------------

@dataclass
class UsageAccumulator:
    """Running totals for one turn, plus a per-model breakdown so a mixed
    provider run can show where the money went."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    #: Model → its own totals. Cost is summed per model, since prices differ.
    by_model: dict[str, CallUsage] = field(default_factory=dict)
    #: None once any call used an unpriced model — a partial total would read
    #: as a complete one.
    _cost: float | None = 0.0
    _cost_known: bool = True
    #: Input tokens of the most recent main-agent call — the live context size,
    #: which is what a context-usage indicator actually wants to show.
    context_tokens: int = 0

    def add(self, usage: CallUsage, *, is_context: bool = False) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        if is_context:
            self.context_tokens = usage.input_tokens

        bucket = self.by_model.setdefault(usage.model, CallUsage(model=usage.model))
        bucket.input_tokens += usage.input_tokens
        bucket.output_tokens += usage.output_tokens
        bucket.cache_read_tokens += usage.cache_read_tokens
        bucket.cache_write_tokens += usage.cache_write_tokens

        cost = usage.cost_usd()
        if cost is None:
            self._cost_known = False
        elif self._cost is not None:
            self._cost += cost

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float | None:
        return self._cost if self._cost_known else None

    def totals(self) -> dict:
        """The shape carried on the `done` event."""
        totals: dict = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "calls": self.calls,
            "context_tokens": self.context_tokens,
        }
        if self.cost_usd is not None:
            totals["cost_usd"] = round(self.cost_usd, 6)
        return totals


_active: ContextVar[UsageAccumulator | None] = ContextVar(
    "daimon_active_usage", default=None
)


def set_active_usage(acc: UsageAccumulator | None) -> None:
    _active.set(acc)


def get_active_usage() -> UsageAccumulator | None:
    return _active.get()


def record(usage: CallUsage | None, *, is_context: bool = False) -> None:
    """Add a call's usage to whatever accumulator is active. A silent no-op
    outside a turn, so instrumenting a call site never needs a guard."""
    if usage is None:
        return
    acc = _active.get()
    if acc is not None:
        acc.add(usage, is_context=is_context)


def format_tokens(n: int) -> str:
    """Compact token count for a status bar: 942, 12.4k, 1.83M."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"
