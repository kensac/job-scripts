"""Admin model-call vocabulary and aggregate statistics."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db, grouped, model_calls
from api.auth import AuthedUser
from api.routers.admin.query_ledger import _distinct_query_values
from api.routers.admin.shared import require_admin
from api.stale_cache import Stale

router = APIRouter()


class QueryVocabulary(BaseModel):
    """The older, unscoped vocabulary: every distinct value ever recorded.
    GET /admin/queries/options is the same idea narrowed to what is still in
    use, and is what the queries page reads."""

    check_types: list[str]
    statuses: list[str]
    configs: list[str]


@router.get("/options")
def options(user: AuthedUser = Depends(require_admin)) -> QueryVocabulary:
    return QueryVocabulary(
        check_types=_distinct_query_values("check_type"),
        statuses=_distinct_query_values("status"),
        configs=_distinct_query_values("config_name"),
    )


class LedgerTotals(BaseModel):
    """Lifetime totals over every model call.

    `cost_usd` is the sum recorded for each call. Historical rows are not
    repriced when the provider datasheet changes.
    """

    queries: int
    unpriced_queries: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cache_write_tokens: int | None
    cache_write_unknown_queries: int
    reasoning_tokens: int
    cost_usd: float


class CheckTypeTotals(BaseModel):
    check_type: str | None
    count: int
    prompt_tokens: int
    completion_tokens: int


class StatusTotals(BaseModel):
    status: str | None
    count: int


class DayTotals(BaseModel):
    """One calendar day in the session timezone, not the first ten characters
    of a timestamp."""

    day: datetime.date
    queries: int
    failed: int
    rejected: int
    unpriced_queries: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cache_write_tokens: int | None
    cache_write_unknown_queries: int
    reasoning_tokens: int


class ModelTotals(BaseModel):
    """What one model has been asked and what that cost.

    The batched columns are retained for usage analysis. `cost_usd` is the
    sum of the per-call values already recorded, so historical rows are not
    silently repriced from aggregate tokens.
    """

    model: str
    queries: int
    unpriced_queries: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cache_write_tokens: int | None
    cache_write_unknown_queries: int
    batched_prompt_tokens: int
    batched_completion_tokens: int
    batched_cached_tokens: int
    cost_usd: float | None


class LedgerStats(BaseModel):
    totals: LedgerTotals
    by_check_type: list[CheckTypeTotals]
    by_status: list[StatusTotals]
    by_day: list[DayTotals]
    by_model: list[ModelTotals]


@router.get("/stats")
def stats(user: AuthedUser = Depends(require_admin)) -> LedgerStats:
    """Lifetime totals over every answer, served stale for
    admin_stats_cache_seconds (api.stale_cache). The dashboard asks on every
    worker event; the totals move by a few rows an hour."""
    return _stats.get()


_STATS_SUMS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "batched_prompt_tokens",
    "batched_completion_tokens",
    "batched_cached_tokens",
    "cost_usd",
)
_STATS_COUNTS = ("queries", "unpriced_queries", "cache_write_unknown_queries")


def _compute_stats() -> LedgerStats:
    """Every cut from ONE scan grouped by (day, model, check_type, status).

    It was five full scans, about 9.6 s on a cache miss against production on
    2026-10-04; one pass measured 3.98 s there. Day is `created_at::date`:
    created_at is timestamptz since a7c1e9d40b22, and ::date makes the bucket
    a real calendar day in the session timezone rather than the first ten
    characters of whatever string the writer produced.

    Answers are read through model_calls.answers_with_usage, not ledger_rows:
    the view's per-row usage lookups suit a page, and a lifetime sum through
    them took 116 s at p90 on production (PostHog, 2026-10-13). Fetches are
    the view's other arm, counted straight off page_fetches.
    """
    groups = db.query(
        f"""
        SELECT created_at::date AS day, model, check_type, status,
               COUNT(*) AS queries,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_queries,
               SUM(prompt_tokens) AS prompt_tokens,
               SUM(completion_tokens) AS completion_tokens,
               SUM(cached_tokens) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COUNT(*) FILTER (WHERE cache_write_tokens IS NULL) AS cache_write_unknown_queries,
               SUM(reasoning_tokens) AS reasoning_tokens,
               SUM(prompt_tokens) FILTER (WHERE batch_id IS NOT NULL) AS batched_prompt_tokens,
               SUM(completion_tokens) FILTER (WHERE batch_id IS NOT NULL) AS batched_completion_tokens,
               SUM(cached_tokens) FILTER (WHERE batch_id IS NOT NULL) AS batched_cached_tokens,
               SUM(cost_usd) AS cost_usd
        FROM (
            SELECT created_at, model, check_type, status, batch_id, prompt_tokens,
                   completion_tokens, cached_tokens, cache_write_tokens,
                   reasoning_tokens, cost_usd
            FROM {model_calls.answers_with_usage("TRUE")} a
            UNION ALL
            SELECT created_at, NULL, 'content', status, NULL, NULL::bigint,
                   NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint, NULL::numeric
            FROM page_fetches WHERE method <> 'verification'
        ) ledger
        GROUP BY 1, 2, 3, 4
        """
    )

    def folded(rows: list[dict[str, Any]]) -> dict[str, Any]:
        acc = grouped.fold(rows, _STATS_SUMS, _STATS_COUNTS)
        # COALESCE(SUM(...), 0) for tokens; cache_write_tokens and cost_usd
        # keep NULL, which means nobody recorded or priced them.
        for k in _STATS_SUMS:
            if k not in ("cache_write_tokens", "cost_usd") and acc[k] is None:
                acc[k] = 0
        return acc

    # Usage is grouped here for the dashboard. Cost is read from the stored
    # per-call values rather than recomputed from aggregate tokens, because
    # rates and cache-write coverage can change after a call was recorded.
    def cost(value: Any) -> float | None:
        return round(float(value), 6) if value is not None else None

    whole = folded(groups)
    totals = grouped.shaped(
        LedgerTotals, whole | {"cost_usd": round(float(whole["cost_usd"] or 0), 6)}
    )
    # Ties in the count had no order in SQL (ORDER BY count alone); the key
    # breaks them now so the order is at least stable.
    by_check_type = sorted(
        (
            grouped.shaped(
                CheckTypeTotals, (a := folded(rows)) | {"check_type": k, "count": a["queries"]}
            )
            for k, rows in grouped.by(groups, "check_type").items()
        ),
        key=lambda r: (-r.count, r.check_type is None, r.check_type or ""),
    )
    by_status = sorted(
        (
            StatusTotals(status=k, count=sum(g["queries"] for g in rows))
            for k, rows in grouped.by(groups, "status").items()
        ),
        key=lambda r: (-r.count, r.status is None, r.status or ""),
    )
    by_day = []
    for day, rows in sorted(grouped.by(groups, "day").items()):
        a = folded(rows)
        a["failed"] = sum(g["queries"] for g in rows if g["status"] == "failed")
        a["rejected"] = sum(g["queries"] for g in rows if g["status"] == "rejected")
        by_day.append(grouped.shaped(DayTotals, a | {"day": day}))
    by_model = sorted(
        (
            grouped.shaped(
                ModelTotals, (a := folded(rows)) | {"model": k, "cost_usd": cost(a["cost_usd"])}
            )
            for k, rows in grouped.by(groups, "model").items()
            if k is not None
        ),
        key=lambda r: (-r.queries, r.model),
    )
    return LedgerStats(
        totals=totals,
        by_check_type=by_check_type,
        by_status=by_status,
        by_day=by_day,
        by_model=by_model,
    )


# The lambda reads _compute_stats at call time, so a test can replace it.
_stats = Stale(
    lambda: _compute_stats(),  # noqa: PLW0108
    lambda: int(db.get_config("admin_stats_cache_seconds")),
)
