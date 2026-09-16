"""Pricing, computed from the provider descriptors.

The rates themselves live in core/providers/, one datasheet per provider, each
number beside its source and the date someone read it. This module is only the
arithmetic - and the two renderings of it, Python for the write path and SQL
for bulk work, held together by a parity test.

It used to own a `{model: (rate_in, rate_out)}` table plus two global
multipliers, one halving batched calls and one charging cached input at a
tenth. That was a faithful model of one vendor's price list rather than of
pricing: the batch discount is not universal (one vendor discounts four of its
seven models and charges the rest full price), and the cached rate is a third
published rate ranging from 10% to 20% to 25% of input, not a fixed fraction
of it.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import NotRequired, TypedDict

from core import providers
from core.providers.spec import Rates, Tier

_PER_MTOK = Decimal(1_000_000)


class RequestTokens(TypedDict):
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cache_write_tokens: NotRequired[int | None]


class _UnspecifiedCacheWrite:
    """Reservation pricing has no provider receipt to supply write tokens."""


_UNSPECIFIED_CACHE_WRITE = _UnspecifiedCacheWrite()


def estimate_usage_cost_usd(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    cached_tokens: int = 0,
    batched: bool = False,
    requests: list[RequestTokens] | None = None,
    cache_write_tokens: int | _UnspecifiedCacheWrite | None = _UNSPECIFIED_CACHE_WRITE,
) -> Decimal | None:
    """Price request boundaries when supplied; aggregate tiered usage is unknown."""
    if requests is None:
        if is_tiered(model):
            return None
        return estimate_cost_usd(
            model,
            prompt_tokens,
            completion_tokens,
            cached_tokens=cached_tokens,
            batched=batched,
            cache_write_tokens=cache_write_tokens,
        )
    if rates_for(model) is None:
        return None
    total = Decimal(0)
    for usage in requests:
        cost = estimate_cost_usd(
            model,
            usage["input_tokens"],
            usage["output_tokens"],
            cached_tokens=usage["cached_tokens"],
            batched=batched,
            cache_write_tokens=usage.get("cache_write_tokens"),
        )
        if cost is None:
            return None
        total += cost
    return total


def rates_for(model: str | None) -> Rates | None:
    entry = providers.model(model)
    return entry.rates if entry else None


def is_tiered(model: str | None) -> bool:
    """True when this model's rates depend on how long the prompt is.

    Tier selection is per REQUEST, so a tiered model cannot be priced from
    summed token counts: a thousand 1K-token calls sum to 1M tokens and select
    a tier no individual call was ever billed at. Callers that aggregate have
    to ask this and decline rather than return a confident wrong number.
    """
    rates = rates_for(model)
    return rates is not None and len(rates.tiers) > 1


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


def off_peak_sql(rates: Rates, at: str) -> str:
    """The same window test as is_off_peak, as a SQL boolean over `at`.

    Rendered from the SAME PeakWindow tuples the Python reads, so a vendor
    changing its hours moves both renderings at once. The hours and days go in
    as literals rather than bound parameters, unlike the rates: they are part
    of the shape of the expression, not numbers a caller supplies, and the
    number of them varies with the provider.

    FALSE when nothing is declared, matching is_off_peak: no discount claimed
    means the tier rate stands.
    """
    if not rates.peak_windows or rates.off_peak_multiplier is None:
        return "FALSE"
    dow = f"EXTRACT(isodow FROM {at} AT TIME ZONE 'UTC')"
    hour = f"EXTRACT(hour FROM {at} AT TIME ZONE 'UTC')"
    windows = " OR ".join(
        f"({dow} IN ({', '.join(str(d) for d in w.isodows)})"
        f" AND {hour} >= {w.start_hour_utc} AND {hour} < {w.end_hour_utc})"
        for w in rates.peak_windows
    )
    return f"NOT ({windows})"


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
    happened. See is_tiered.

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
    off_peak: str = "FALSE",
    off_peak_multiplier: str = "1",
) -> str:
    """The same formula as estimate_cost_usd, rendered as a SQL expression.

    Bulk pricing (a backfill over 74k rows, an aggregate over a time window)
    has to happen in the database; pulling every row into Python to price it
    would be absurd. But a second hand-written copy of the formula is exactly
    the drift this module exists to end, so both renderings are generated here
    and tested against each other.

    Every rate is a placeholder the caller binds, including the cached rate and
    the batch multiplier that used to be module-level constants. That is the
    point: there is no longer a universal discount to hardcode on either side.
    A caller whose model has no batch lane binds 1.

    `off_peak` is a SQL boolean - build it with off_peak_sql() rather than by
    hand, so the window definition has one source. It defaults to FALSE, which
    is what every provider without wall-clock pricing wants and leaves their
    callers unchanged.

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
    if cache_write is None:
        write_part = "0"
        write_rate = model_rate_in
        unknown_guard = ""
    else:
        write_part = f"LEAST(GREATEST(COALESCE({cache_write}, 0), 0), {remaining})"
        write_rate = (
            f"COALESCE({model_rate_cache_write_in}, {model_rate_in})"
            if model_rate_cache_write_in is not None
            else model_rate_in
        )
        unknown_conditions = []
        if model_rate_cache_write_in is not None:
            unknown_conditions.append(
                f"{cache_write} IS NULL AND {model_rate_cache_write_in} IS NOT NULL "
                f"AND {model_rate_cache_write_in} <> {model_rate_in}"
            )
        unknown_conditions.append(
            f"{write_part} > 0"
            + (
                f" AND {model_rate_cache_write_in} IS NULL"
                if model_rate_cache_write_in is not None
                else ""
            )
        )
        unknown_guard = f"CASE WHEN {' OR '.join(unknown_conditions)} THEN NULL ELSE "
    ordinary = f"({remaining} - {write_part})"
    expression = (
        f"(({ordinary} * {model_rate_in}"
        f" + {cached_part} * {model_rate_cached_in}"
        f" + {write_part} * {write_rate}"
        f" + {c} * {model_rate_out}) / {_PER_MTOK})"
        f" * CASE WHEN {batched} THEN {batch_rate} ELSE 1 END"
        f" * CASE WHEN {off_peak} THEN {off_peak_multiplier} ELSE 1 END"
    )
    return f"{unknown_guard}{expression} END" if unknown_guard else expression
