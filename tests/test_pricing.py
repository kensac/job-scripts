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

# gpt-5-nano is $0.05/Mtok in, $0.40/Mtok out - the model almost everything
# here runs on, so the arithmetic below is checkable by hand.
NANO = "gpt-5-nano"


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


_CASES = [
    (1_000_000, 0, 0, False),
    (0, 1_000_000, 0, False),
    (1_000_000, 1_000_000, 0, True),
    (1_000_000, 250_000, 400_000, False),
    (1_000_000, 250_000, 400_000, True),
    (3, 7, 1, False),
    (0, 0, 0, True),
    (999_999, 1, 999_999, True),
]


@pytest.mark.parametrize(("prompt", "completion", "cached", "batched"), _CASES)
def test_sql_and_python_agree(prompt, completion, cached, batched):
    """The migration prices 74k rows in SQL and the write path prices each new
    row in Python. Two renderings of one formula; if they disagree, historical
    spend and live spend are measured differently and no chart is trustworthy.
    """
    rate_in, rate_out = pricing.PRICES_PER_MTOK[NANO]
    expr = pricing.cost_sql(
        model_rate_in="%(rate_in)s::numeric",
        model_rate_out="%(rate_out)s::numeric",
        prompt="%(prompt)s::bigint",
        completion="%(completion)s::bigint",
        cached="%(cached)s::bigint",
        batched="%(batched)s::boolean",
    )
    row = db.query_one(
        f"SELECT {expr} AS cost",
        {
            "rate_in": str(rate_in),
            "rate_out": str(rate_out),
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
