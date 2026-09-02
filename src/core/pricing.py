from __future__ import annotations

from decimal import Decimal

# USD per 1M tokens (input, output); models absent here emit no cost.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    # Sonnet 5's launch intro price ($2/$10) became the standard price.
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# The Batch API bills at half the synchronous rate. This is the whole reason
# scheduled work parks on a batch instead of holding a worker, so the number
# belongs next to the prices it halves rather than inline at the call site.
BATCH_MULTIPLIER = Decimal("0.5")

# Cached input tokens bill at 10% of the input rate. `cached_tokens` is a
# SUBSET of `prompt_tokens` (the provider reports it under
# prompt_tokens_details), so the uncached remainder is the difference - not
# the whole prompt.
CACHED_INPUT_MULTIPLIER = Decimal("0.1")

_PER_MTOK = Decimal(1_000_000)


def estimate_cost_usd(
    model: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    *,
    cached_tokens: int | None = None,
    batched: bool = False,
) -> Decimal | None:
    """Cost of one call, or None when the model has no published price.

    Returning None rather than 0 keeps "we do not know what this cost" distinct
    from "this was free"; summing a column of NULLs tells you coverage is
    incomplete, summing a column of zeros silently understates the bill.
    """
    price = PRICES_PER_MTOK.get(model or "")
    if price is None:
        return None
    prompt = Decimal(prompt_tokens or 0)
    completion = Decimal(completion_tokens or 0)
    cached = min(Decimal(cached_tokens or 0), prompt)
    rate_in, rate_out = Decimal(str(price[0])), Decimal(str(price[1]))
    cost = (
        (prompt - cached) * rate_in
        + cached * rate_in * CACHED_INPUT_MULTIPLIER
        + completion * rate_out
    ) / _PER_MTOK
    if batched:
        cost *= BATCH_MULTIPLIER
    return cost


def cost_sql(
    *,
    model_rate_in: str,
    model_rate_out: str,
    prompt: str = "prompt_tokens",
    completion: str = "completion_tokens",
    cached: str = "cached_tokens",
    batched: str,
) -> str:
    """The same formula as estimate_cost_usd, rendered as a SQL expression.

    Bulk pricing (a backfill over 74k rows, an aggregate over a time window)
    has to happen in the database; pulling every row into Python to price it
    would be absurd. But a second hand-written copy of the formula is exactly
    the drift this module exists to end, so both renderings are generated
    here and tested against each other.

    Callers pass parameter placeholders for the rates, not literals, so the
    price table stays the single source of the numbers.
    """
    p, c, k = f"COALESCE({prompt}, 0)", f"COALESCE({completion}, 0)", f"COALESCE({cached}, 0)"
    uncached = f"({p} - LEAST({k}, {p}))"
    return (
        f"(({uncached} * {model_rate_in}"
        f" + LEAST({k}, {p}) * {model_rate_in} * {CACHED_INPUT_MULTIPLIER}"
        f" + {c} * {model_rate_out}) / {_PER_MTOK})"
        f" * CASE WHEN {batched} THEN {BATCH_MULTIPLIER} ELSE 1 END"
    )
