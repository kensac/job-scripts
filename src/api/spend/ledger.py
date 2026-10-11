"""Recorded model-call ledger aggregates and response shapes."""

import datetime
from typing import Any

from pydantic import BaseModel

from api import db
from api.spend.window import _WINDOW


class LedgerBucket(BaseModel):
    """One grouping set of the usage ledger. The three below are the same
    measures cut three ways, so the dimension each carries is the only thing
    that differs."""

    calls: int
    ledger_rows: int
    priced_calls: int
    unpriced_calls: int
    unknown_model_calls: int
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    cache_write_tokens: int | None
    cache_write_unknown_calls: int
    batched_calls: int
    models: int
    first_call: datetime.datetime | None
    last_call: datetime.datetime | None


class LedgerPurpose(LedgerBucket):
    purpose: str


class LedgerModel(LedgerBucket):
    # Null is a call whose model was never recorded; unknown_model_calls
    # counts them rather than dropping them.
    model: str | None


class LedgerDay(LedgerBucket):
    day: datetime.date


class Ledger(BaseModel):
    """Recorded usage, labelled with what it is and is not."""

    source: str
    basis: str
    timezone: str
    window_days: int
    note: str
    totals: LedgerBucket
    by_purpose: list[LedgerPurpose]
    by_model: list[LedgerModel]
    by_day: list[LedgerDay]


class LedgerWithTotals(Ledger):
    """The ledger with the two populations' totals side by side. They answer
    different questions and stay labelled rather than forced to agree."""

    spend_total_usd: float
    verdict_total_usd: float | None


def read_ledger_breakdowns(params: dict) -> Ledger:
    # One snapshot keeps the breakdowns reconcilable. Reduce to daily model
    # groups before rolling up: DISTINCT model across raw grouping sets caused
    # a 160 ms ledger query on 99,831 duplicated corpus rows versus 54 ms for the old
    # purpose-only query. Keep that volume in view when changing this plan.
    rows = db.query(
        f"""
        WITH daily_models AS (
            SELECT purpose, NULLIF(BTRIM(model), '') AS model,
                   (created_at AT TIME ZONE 'UTC')::date AS day,
                   SUM(requests) AS calls,
                   COUNT(*) AS ledger_rows,
                   COALESCE(SUM(requests) FILTER (WHERE cost_usd IS NOT NULL), 0) AS priced_calls,
                   COALESCE(SUM(requests) FILTER (WHERE cost_usd IS NULL), 0) AS unpriced_calls,
                   COALESCE(SUM(requests) FILTER (WHERE NULLIF(BTRIM(model), '') IS NULL), 0)
                       AS unknown_model_calls,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   COALESCE(SUM(requests) FILTER (WHERE cache_write_tokens IS NULL), 0)
                       AS cache_write_unknown_calls,
                   COALESCE(SUM(requests) FILTER (WHERE batched), 0) AS batched_calls,
                   MIN(created_at) AS first_call,
                   MAX(created_at) AS last_call
            FROM model_calls WHERE {_WINDOW}
            GROUP BY 1, 2, 3
        )
        SELECT purpose, model, day, GROUPING(purpose, model, day) AS grouping,
               COALESCE(SUM(calls), 0)::bigint AS calls,
               COALESCE(SUM(ledger_rows), 0)::bigint AS ledger_rows,
               COALESCE(SUM(priced_calls), 0)::bigint AS priced_calls,
               COALESCE(SUM(unpriced_calls), 0)::bigint AS unpriced_calls,
               COALESCE(SUM(unknown_model_calls), 0)::bigint AS unknown_model_calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COALESCE(SUM(cache_write_unknown_calls), 0)::bigint AS cache_write_unknown_calls,
               COALESCE(SUM(batched_calls), 0)::bigint AS batched_calls,
               COUNT(DISTINCT model) AS models,
               MIN(first_call) AS first_call,
               MAX(last_call) AS last_call
        FROM daily_models
        GROUP BY GROUPING SETS ((), (purpose), (model), (day))
        """,
        params,
    )
    # Which dimension a row carries, and the shape that says so. The other
    # two columns are null on that row and are dropped rather than declared.
    dimensions = {3: ("purpose", LedgerPurpose), 5: ("model", LedgerModel), 6: ("day", LedgerDay)}
    buckets: dict[str, list[Any]] = {"purpose": [], "model": [], "day": []}
    totals = None
    for row in rows:
        carried = dimensions.get(row.pop("grouping"))
        for name in ("purpose", "model", "day"):
            if carried is None or name != carried[0]:
                row.pop(name)
        if carried is None:
            totals = LedgerBucket(**row)
        else:
            buckets[carried[0]].append(carried[1](**row))
    # An aggregate over the empty grouping set returns its row whatever the
    # window holds.
    assert totals
    buckets["purpose"].sort(key=lambda r: (-r.cost_usd, r.purpose))
    buckets["model"].sort(key=lambda r: (-r.cost_usd, r.model or ""))
    buckets["day"].sort(key=lambda r: r.day)
    return Ledger(
        source="model_calls",
        basis="recorded_estimate",
        timezone="UTC",
        window_days=params["days"],
        note=(
            "Costs sum the estimate stored for each paid call when it was made, not "
            "provider invoices or proof that every call was recorded. Unpriced calls are "
            "excluded from costs and counted separately. calls counts provider requests; "
            "ledger_rows counts rows, one of which can stand for a whole batch that kept "
            "no per-request record (before 2026-09-09). Windows reaching before "
            "2026-09-13 show less than the old usage ledger did: it priced batched filter "
            "work at live rates, and these are the batch rates that were paid."
        ),
        totals=totals,
        by_purpose=buckets["purpose"],
        by_model=buckets["model"],
        by_day=buckets["day"],
    )
