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
    """Return the cached-input rate, falling back to full input rate when unpublished."""
    return tier.rate_in if tier.rate_cached_in is None else tier.rate_cached_in


def cache_write_rate(tier: Tier) -> Decimal | None:
    """Return the published rate for input tokens written to the cache."""
    return tier.rate_cache_write_in


def is_off_peak(rates: Rates, at: datetime.datetime | None) -> bool:
    """Return whether `at` (UTC, isodow 1=Monday) falls outside all declared peak windows.

    isodow is converted here and only here so the SQL rendering and this one agree.

    Returns False when `at`, peak windows, or off-peak multiplier is missing so
    undiscounted peak rates apply by default.
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
    """Price a single call, or return None when the model or cache-write cost is unknown.

    None, not 0: summed zeros silently understate the bill, summed NULLs show
    incomplete coverage.

    Must be called per request because prompt-length tier selection depends on a
    single request's prompt tokens. Omitting `at` bills peak rates rather than
    reading the wall clock.
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
        # Pre-call reservation has no receipt; conservatively assume all uncached tokens write.
        cache_write = remaining if write_rate is not None else Decimal(0)
    elif cache_write_tokens is None:
        # Receipt omitted cache writes; return None if the model charges a premium write rate.
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
    # A batched call with no batch lane bills at the synchronous rate; a discount
    # there would under-report cost.
    if batched and rates.batch_rate is not None:
        cost *= rates.batch_rate
    if is_off_peak(rates, at) and rates.off_peak_multiplier is not None:
        cost *= rates.off_peak_multiplier
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
    """Render `estimate_cost_usd` as a SQL expression for bulk pricing.

    Both renderings live here and are tested against each other; never hand-copy
    the formula elsewhere.

    Callers bind per-tier rates (and 1 for `batch_rate` when a model has no batch
    lane), filter rows by tier prompt range when needed, and handle wall-clock
    off-peak discounts outside SQL.
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
