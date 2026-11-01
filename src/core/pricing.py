"""Price model calls from the provider datasheets in ``core.providers``.

Each datasheet owns its sourced rates. This module applies those rates in
Python and SQL; a parity test keeps the two calculations aligned. Batch and
cached-input rates come from each provider and model, not global discounts.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from core import providers
from core.providers.spec import Rates, Tier

_PER_MTOK = Decimal(1_000_000)


class _UnspecifiedCacheWrite:
    """Reservation pricing has no provider receipt to supply write tokens."""


_UNSPECIFIED_CACHE_WRITE = _UnspecifiedCacheWrite()


def rates_for(model: str | None) -> Rates | None:
    entry = providers.model(model)
    return entry.rates if entry else None


def _tier_for(rates: Rates, prompt_tokens: Decimal) -> Tier:
    for tier in rates.tiers:
        if tier.up_to_prompt_tokens is None or prompt_tokens <= tier.up_to_prompt_tokens:
            return tier
    return rates.tiers[-1]


def cached_rate(tier: Tier) -> Decimal:
    """A cache hit's rate, or the full input rate when the vendor publishes none.

    Alibaba, for two of its models, names them explicitly and declines to give
    a number. Billing those at the input rate overstates a cache hit rather
    than inventing a discount - the same reasoning that books an unpriced model
    as NULL instead of zero.
    """
    return tier.rate_in if tier.rate_cached_in is None else tier.rate_cached_in


def cache_write_rate(tier: Tier) -> Decimal | None:
    """Return the published rate for input tokens written to the cache."""
    return tier.rate_cache_write_in


def is_off_peak(rates: Rates, at: datetime.datetime | None) -> bool:
    """Whether `at` falls outside every declared peak window.

    False whenever the answer is not knowable: no timestamp, no declared
    windows, or no published discount. That is the safe direction - the tier
    rates ARE the peak rates, so returning False bills the undiscounted price
    rather than inventing a discount, exactly as cached_rate() does when a
    vendor publishes no cached rate.

    isodow, 1=Monday, converted here and only here so the SQL rendering and
    this one cannot disagree about which day is which.
    """
    if at is None or not rates.peak_windows or rates.off_peak_multiplier is None:
        return False
    utc = at.astimezone(datetime.UTC)
    isodow, hour = utc.isoweekday(), utc.hour
    return not any(
        isodow in w.isodows and w.start_hour_utc <= hour < w.end_hour_utc
        for w in rates.peak_windows
    )


def estimate_cost_usd(
    model: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    *,
    cached_tokens: int | None = None,
    batched: bool = False,
    at: datetime.datetime | None = None,
    cache_write_tokens: int | _UnspecifiedCacheWrite | None = _UNSPECIFIED_CACHE_WRITE,
) -> Decimal | None:
    """Cost of ONE call, or None when the model has no published price.

    Returning None rather than 0 keeps "we do not know what this cost" distinct
    from "this was free"; summing a column of NULLs tells you coverage is
    incomplete, summing a column of zeros silently understates the bill.

    One call, not many: the tier is chosen by this request's prompt length, so
    passing summed tokens for a tiered model prices a request that never
    happened.

    `at` is when the request was made, for the vendors that bill by wall-clock
    time. Omitting it bills the PEAK rate rather than reading the clock here:
    the function stays deterministic and testable, and a caller that does not
    know the time overstates instead of guessing a discount. Every caller that
    matters does know it - cost is written at call time.
    """
    rates = rates_for(model)
    if rates is None:
        return None
    prompt = max(Decimal(prompt_tokens or 0), Decimal(0))
    completion = max(Decimal(completion_tokens or 0), Decimal(0))
    cached = min(max(Decimal(cached_tokens or 0), Decimal(0)), prompt)
    remaining = max(prompt - cached, Decimal(0))
    tier = _tier_for(rates, prompt)
    write_rate = cache_write_rate(tier)
    if cache_write_tokens is _UNSPECIFIED_CACHE_WRITE:
        # A reservation has no provider receipt. Reserve for every uncached
        # token being written, which is the conservative GPT-5.6+ case.
        cache_write = remaining if write_rate is not None else Decimal(0)
    elif cache_write_tokens is None:
        # A receipt that omits the field is unknown. If the model publishes a
        # premium write rate, returning NULL avoids inventing zero writes.
        if write_rate is not None and write_rate != tier.rate_in:
            return None
        cache_write = Decimal(0)
    else:
        assert isinstance(cache_write_tokens, int)
        cache_write = min(max(Decimal(cache_write_tokens), Decimal(0)), remaining)
    if cache_write and write_rate is None:
        return None
    cost = (
        (remaining - cache_write) * tier.rate_in
        + cached * cached_rate(tier)
        + cache_write * (write_rate if write_rate is not None else tier.rate_in)
        + completion * tier.rate_out
    ) / _PER_MTOK
    if batched and rates.batch_rate is not None:
        cost *= rates.batch_rate
    if is_off_peak(rates, at) and rates.off_peak_multiplier is not None:
        cost *= rates.off_peak_multiplier
    # A batched call on a model whose vendor publishes no batch lane bills at
    # the synchronous rate, so there is nothing to apply. Applying a discount
    # here anyway would report a fraction of the true cost on the one surface
    # built to make cost visible - a wrong number, which is worse than the NULL
    # an unpriced model gets.
    return cost


def cost_sql(
    *,
    model_rate_in: str,
    model_rate_out: str,
    model_rate_cached_in: str,
    batch_rate: str,
    model_rate_cache_write_in: str | None = None,
    prompt: str = "prompt_tokens",
    completion: str = "completion_tokens",
    cached: str = "cached_tokens",
    cache_write: str | None = None,
    batched: str,
) -> str:
    """The same formula as estimate_cost_usd, rendered as a SQL expression.

    Bulk pricing (a backfill over 74k rows, an aggregate over a time window)
    has to happen in the database; pulling every row into Python to price it
    would be absurd. But a second hand-written copy of the formula is exactly
    the drift this module exists to end, so both renderings are generated here
    and tested against each other.

    Every rate is a placeholder the caller binds, including the cached rate and
    the batch multiplier, because no discount is universal across models. A
    caller whose model has no batch lane binds 1.

    Wall-clock discounts are NOT rendered here either: estimate_cost_usd
    applies them from `at`, and no bulk caller prices a provider that has them.

    Tier selection is NOT rendered here. A tiered model's rate depends on each
    row's own prompt length, so a caller pricing in bulk binds the rates per
    tier and restricts each statement to the matching prompt range - the way
    the backfill already issues one statement per model.
    """
    p = f"GREATEST(COALESCE({prompt}, 0), 0)"
    c = f"GREATEST(COALESCE({completion}, 0), 0)"
    k = f"COALESCE({cached}, 0)"
    cached_part = f"LEAST(GREATEST({k}, 0), {p})"
    remaining = f"GREATEST({p} - {cached_part}, 0)"
    unknown = ""
    if cache_write is None:
        write_part = "0"
        write_rate = model_rate_in
    else:
        write_part = f"LEAST(GREATEST(COALESCE({cache_write}, 0), 0), {remaining})"
        if model_rate_cache_write_in is None:
            # No write rate to bind, so any reported write is unpriced.
            write_rate = model_rate_in
            unknown = f"{write_part} > 0"
        else:
            # Unknown, as in estimate_cost_usd, when a receipt omits writes on a
            # model with a premium write rate, or reports writes on a model with
            # no published write rate.
            rate = model_rate_cache_write_in
            write_rate = f"COALESCE({rate}, {model_rate_in})"
            unknown = (
                f"({cache_write} IS NULL AND {rate} IS NOT NULL AND {rate} <> {model_rate_in})"
                f" OR ({write_part} > 0 AND {rate} IS NULL)"
            )
    ordinary = f"({remaining} - {write_part})"
    expression = (
        f"(({ordinary} * {model_rate_in}"
        f" + {cached_part} * {model_rate_cached_in}"
        f" + {write_part} * {write_rate}"
        f" + {c} * {model_rate_out}) / {_PER_MTOK})"
        f" * CASE WHEN {batched} THEN {batch_rate} ELSE 1 END"
    )
    return f"CASE WHEN {unknown} THEN NULL ELSE {expression} END" if unknown else expression
