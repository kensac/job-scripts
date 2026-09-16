"""Cost is money, so the formula gets exact-value assertions.

The Python and SQL renderings are generated from one place (core.pricing), but
"generated from one place" is a claim, not a guarantee - the parity test at the
bottom is what actually holds them together.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from api import db
from core import pricing
from core.providers.spec import Rates, Tier

# gpt-5-nano is $0.05/Mtok in, $0.40/Mtok out - the model almost everything
# here runs on, so the arithmetic below is checkable by hand.
NANO = "gpt-5-nano"
LUNA = "gpt-5.6-luna"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(prompt_tokens=1_000_000, completion_tokens=0), "0.05"),
        (dict(prompt_tokens=0, completion_tokens=1_000_000), "0.40"),
        (dict(prompt_tokens=1_000_000, completion_tokens=1_000_000), "0.45"),
        # Batch API bills at half.
        (dict(prompt_tokens=1_000_000, completion_tokens=1_000_000, batched=True), "0.225"),
        # Cached input is a SUBSET of prompt_tokens at 10% of the input rate:
        # a fully cached 1M prompt costs 0.05 * 0.1.
        (dict(prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=1_000_000), "0.005"),
        # Half cached: 500k at full rate + 500k at a tenth.
        (dict(prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=500_000), "0.0275"),
        (dict(prompt_tokens=0, completion_tokens=0), "0"),
    ],
)
def test_known_costs(kwargs, expected):
    got = pricing.estimate_cost_usd(NANO, **kwargs)
    assert got is not None
    assert got == Decimal(expected)


def test_unpriced_model_is_none_not_zero():
    """None and 0 mean different things: 'we cannot price this' must not be
    summed into a total as if the call were free."""
    assert pricing.estimate_cost_usd("no-such-model", 1_000_000, 1_000_000) is None
    assert pricing.estimate_cost_usd(None, 1_000_000, 1_000_000) is None
    assert pricing.estimate_cost_usd(NANO, 0, 0) == 0


def test_cached_cannot_exceed_prompt():
    """A provider reporting more cached tokens than prompt tokens must not
    produce a negative (i.e. a credit)."""
    cost = pricing.estimate_cost_usd(NANO, 100, 0, cached_tokens=10**9)
    assert cost is not None and cost > 0


def test_none_tokens_are_zero_not_a_crash():
    assert pricing.estimate_cost_usd(NANO, None, None) == 0


def test_cache_write_is_distinct_from_a_cache_read_and_unknown_is_not_free():
    assert pricing.estimate_cost_usd(LUNA, 1_000_000, 0, cache_write_tokens=1_000_000) == Decimal(
        "0.25"
    )
    assert pricing.estimate_cost_usd(LUNA, 1_000_000, 0, cache_write_tokens=0) == Decimal("0.20")
    assert pricing.estimate_cost_usd(LUNA, 1_000_000, 0, cache_write_tokens=None) is None
    # A reservation has no receipt, so it reserves the published write rate
    # rather than silently assuming that no tokens will be written.
    assert pricing.estimate_cost_usd(LUNA, 1_000_000, 0) == Decimal("0.25")


_CASES = [
    (1_000_000, 0, 0, False),
    (0, 1_000_000, 0, False),
    (1_000_000, 1_000_000, 0, True),
    (1_000_000, 250_000, 400_000, False),
    (1_000_000, 250_000, 400_000, True),
    (3, 7, 1, False),
    (0, 0, 0, True),
    (999_999, 1, 999_999, True),
    (1_000_000, -100, -500, False),
    (1_000_000, 100, 2_000_000, False),
]


@pytest.mark.parametrize(("prompt", "completion", "cached", "batched"), _CASES)
def test_sql_and_python_agree(prompt, completion, cached, batched):
    """The migration prices 74k rows in SQL and the write path prices each new
    row in Python. Two renderings of one formula; if they disagree, historical
    spend and live spend are measured differently and no chart is trustworthy.
    """
    price = pricing.rates_for(NANO)
    assert price is not None
    tier = price.tiers[0]
    expr = pricing.cost_sql(
        model_rate_in="%(rate_in)s::numeric",
        model_rate_out="%(rate_out)s::numeric",
        model_rate_cached_in="%(rate_cached)s::numeric",
        batch_rate="%(batch_rate)s::numeric",
        prompt="%(prompt)s::bigint",
        completion="%(completion)s::bigint",
        cached="%(cached)s::bigint",
        batched="%(batched)s::boolean",
    )
    row = db.query_one(
        f"SELECT {expr} AS cost",
        {
            "rate_in": str(tier.rate_in),
            "rate_out": str(tier.rate_out),
            "rate_cached": str(pricing.cached_rate(tier)),
            "batch_rate": str(price.batch_rate if price.batch_rate is not None else 1),
            "prompt": prompt,
            "completion": completion,
            "cached": cached,
            "batched": batched,
        },
    )
    assert row is not None
    py = pricing.estimate_cost_usd(NANO, prompt, completion, cached_tokens=cached, batched=batched)
    assert py is not None
    assert Decimal(row["cost"]) == py


def test_sql_and_python_agree_with_cache_write_tokens():
    price = pricing.rates_for(LUNA)
    assert price is not None
    tier = price.tiers[0]
    expr = pricing.cost_sql(
        model_rate_in="%(rate_in)s::numeric",
        model_rate_out="%(rate_out)s::numeric",
        model_rate_cached_in="%(rate_cached)s::numeric",
        model_rate_cache_write_in="%(rate_write)s::numeric",
        batch_rate="%(batch_rate)s::numeric",
        prompt="%(prompt)s::bigint",
        completion="%(completion)s::bigint",
        cached="%(cached)s::bigint",
        cache_write="%(cache_write)s::bigint",
        batched="%(batched)s::boolean",
    )
    row = db.query_one(
        f"SELECT {expr} AS cost",
        {
            "rate_in": str(tier.rate_in),
            "rate_out": str(tier.rate_out),
            "rate_cached": str(pricing.cached_rate(tier)),
            "rate_write": str(pricing.cache_write_rate(tier)),
            "batch_rate": str(price.batch_rate if price.batch_rate is not None else 1),
            "prompt": 1_000_000,
            "completion": 100_000,
            "cached": 300_000,
            "cache_write": 500_000,
            "batched": True,
        },
    )
    assert row is not None
    py = pricing.estimate_cost_usd(
        LUNA,
        1_000_000,
        100_000,
        cached_tokens=300_000,
        cache_write_tokens=500_000,
        batched=True,
    )
    assert py is not None
    assert Decimal(row["cost"]) == py


@pytest.mark.parametrize(
    ("cached", "cache_write"),
    [(-100, -200), (2_000_000, 2_000_000), (500_000, 2_000_000)],
)
def test_sql_and_python_clamp_cache_counters(cached, cache_write):
    price = pricing.rates_for(LUNA)
    assert price is not None
    tier = price.tiers[0]
    expr = pricing.cost_sql(
        model_rate_in="%(rate_in)s::numeric",
        model_rate_out="%(rate_out)s::numeric",
        model_rate_cached_in="%(rate_cached)s::numeric",
        model_rate_cache_write_in="%(rate_write)s::numeric",
        batch_rate="1",
        prompt="%(prompt)s::bigint",
        completion="%(completion)s::bigint",
        cached="%(cached)s::bigint",
        cache_write="%(cache_write)s::bigint",
        batched="FALSE",
    )
    row = db.query_one(
        f"SELECT {expr} AS cost",
        {
            "rate_in": str(tier.rate_in),
            "rate_out": str(tier.rate_out),
            "rate_cached": str(pricing.cached_rate(tier)),
            "rate_write": str(pricing.cache_write_rate(tier)),
            "prompt": 1_000_000,
            "completion": 100,
            "cached": cached,
            "cache_write": cache_write,
        },
    )
    assert row is not None
    py = pricing.estimate_cost_usd(
        LUNA,
        1_000_000,
        100,
        cached_tokens=cached,
        cache_write_tokens=cache_write,
    )
    assert py is not None
    assert Decimal(row["cost"]) == py


def test_zero_cache_write_rate_is_not_treated_as_missing(monkeypatch):
    base = pricing.rates_for(NANO)
    assert base is not None
    rates = Rates(
        tiers=(Tier(None, Decimal("1"), Decimal("2"), Decimal("0.1"), Decimal("0")),),
        batch_rate=Decimal("1"),
        source=base.source,
    )
    monkeypatch.setattr(pricing, "rates_for", lambda model: rates)
    py = pricing.estimate_cost_usd("zero-write", 1_000_000, 0, cache_write_tokens=1_000_000)
    assert py == Decimal("0")
    expr = pricing.cost_sql(
        model_rate_in="1::numeric",
        model_rate_out="2::numeric",
        model_rate_cached_in="0.1::numeric",
        model_rate_cache_write_in="0::numeric",
        batch_rate="1",
        prompt="1000000::bigint",
        completion="0::bigint",
        cached="0::bigint",
        cache_write="1000000::bigint",
        batched="FALSE",
    )
    row = db.query_one(f"SELECT {expr} AS cost")
    assert row is not None and Decimal(row["cost"]) == py


@pytest.mark.parametrize(
    ("write_rate", "cache_write", "expected"),
    [(None, 500_000, None), (None, 0, Decimal("0.20")), ("0.25", None, None)],
)
def test_sql_unknown_write_rate_matches_python(write_rate, cache_write, expected):
    expr = pricing.cost_sql(
        model_rate_in="0.20::numeric",
        model_rate_out="12::numeric",
        model_rate_cached_in="0.02::numeric",
        model_rate_cache_write_in=(
            "%(rate_write)s::numeric" if write_rate is not None else "NULL::numeric"
        ),
        batch_rate="1",
        prompt="1000000::bigint",
        completion="0::bigint",
        cached="0::bigint",
        cache_write="%(cache_write)s::bigint",
        batched="FALSE",
    )
    row = db.query_one(
        f"SELECT {expr} AS cost", {"rate_write": write_rate, "cache_write": cache_write}
    )
    assert row is not None
    assert row["cost"] == expected
