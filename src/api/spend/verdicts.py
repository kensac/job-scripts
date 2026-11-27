"""Posting-verdict cost diagnostics, separate from model-call spend."""

import datetime
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel

from api import db, grouped, ledger_rollup, model_calls
from api.spend.window import _ANSWERS, _WINDOW, _WINDOW_START
from core.store import AI_ELIGIBLE_JOB


class VerdictTotals(BaseModel):
    cost_usd: float
    calls: int
    unpriced_calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cache_write_tokens: int | None
    cache_write_unknown_calls: int
    reasoning_tokens: int
    first_call: datetime.datetime | None
    last_call: datetime.datetime | None


class BatchingDiagnostics(BaseModel):
    """A retained compatibility estimate. Missing batch IDs do not prove
    synchronous transport, which is why the basis travels with the numbers."""

    batched_calls: int
    sync_calls: int
    batched_cost_usd: float
    sync_cost_usd: float
    batchable_sync_calls: int
    unrealized_savings_usd: float
    basis: str = "verdict_metadata_hypothesis"
    note: str = (
        "Missing batch IDs do not establish synchronous transport. "
        "unrealized_savings_usd is a legacy half-cost scenario, not verified savings."
    )


class VerdictCheckTypeSpend(BaseModel):
    check_type: str | None
    calls: int
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cached_tokens: int
    cache_write_tokens: int | None
    cache_write_unknown_calls: int
    batched_calls: int
    # Decided verdicts carrying no tokens: the answer came from a sibling
    # row's call, so their cost lives there.
    joint_call_rows: int


class VerdictReachSpend(BaseModel):
    """Whether the work was for a posting anybody could still be shown."""

    reach: str
    calls: int
    unpriced_calls: int
    cost_usd: float
    total_tokens: int


class VerdictSourceReachSpend(VerdictReachSpend):
    source: str


class VerdictModelSpend(BaseModel):
    model: str
    calls: int
    cost_usd: float
    total_tokens: int
    unpriced_calls: int


class VerdictDaySpend(BaseModel):
    day: datetime.date
    cost_usd: float
    batched_cost_usd: float
    sync_cost_usd: float
    calls: int


class Waste(BaseModel):
    """A diagnostic, not proof that earlier work was unnecessary."""

    failed_calls: int
    failed_cost_usd: float
    superseded_verdicts: int
    superseded_cost_usd: float


class VerdictDiagnostics(BaseModel):
    """The verdict log's own view of spend, labelled apart from the ledger
    because rows here are posting verdicts, not unique provider calls."""

    source: str
    basis: str
    note: str
    totals: VerdictTotals
    batching: BatchingDiagnostics
    by_check_type: list[VerdictCheckTypeSpend]
    by_reach: list[VerdictReachSpend]
    by_source_reach: list[VerdictSourceReachSpend]
    by_model: list[VerdictModelSpend]
    by_day: list[VerdictDaySpend]
    waste: Waste


# Every verdict sum is COALESCE(SUM(...), 0) except cache_write_tokens, whose
# NULL means nobody recorded it.
_VERDICT_SUMS = (
    "cost_usd",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "batched_cost_usd",
    "sync_cost_usd",
    "unrealized_savings_usd",
)
_VERDICT_COUNTS = (
    "calls",
    "unpriced_calls",
    "cache_write_unknown_calls",
    "batched_calls",
    "sync_calls",
    "batchable_sync_calls",
    "joint_call_rows",
)


def read_verdict_breakdowns(
    params: dict,
) -> tuple[
    VerdictTotals,
    BatchingDiagnostics,
    list[VerdictCheckTypeSpend],
    list[VerdictModelSpend],
    list[VerdictDaySpend],
]:
    """Read totals, batching, check_type, model, and day cuts in one rollup scan.

    Uses `api.ledger_rollup` hourly sums grouped by `(day, model, check_type)`
    and folds Decimals in Python so all cuts stay consistent without rescanning
    `ai_queries` (measured 97s across five raw scans on 2026-10-04, where one
    grouped pass took 3.2-4.0s for 327 groups; 15s single raw scan on 2026-10-13).
    Day is `hour::date`, exact in any whole-hour timezone. Decimal sums keep a sum
    of group sums equal to one SUM.
    """
    groups = db.query(
        f"""
        SELECT hour::date AS day, model, check_type,
               SUM(answers) AS calls,
               SUM(unpriced) AS unpriced_calls,
               SUM(cost_usd) AS cost_usd,
               SUM(prompt_tokens) AS prompt_tokens,
               SUM(completion_tokens) AS completion_tokens,
               SUM(total_tokens) AS total_tokens,
               SUM(cached_tokens) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               SUM(cache_write_unknown) AS cache_write_unknown_calls,
               SUM(reasoning_tokens) AS reasoning_tokens,
               MIN(first_at) AS first_call,
               MAX(last_at) AS last_call,
               COALESCE(SUM(answers) FILTER (WHERE batched), 0) AS batched_calls,
               COALESCE(SUM(answers) FILTER (WHERE NOT batched), 0) AS sync_calls,
               SUM(cost_usd) FILTER (WHERE batched) AS batched_cost_usd,
               SUM(cost_usd) FILTER (WHERE NOT batched) AS sync_cost_usd,
               COALESCE(SUM(answers) FILTER (
                   WHERE NOT batched AND COALESCE(config_name, '') <> ALL(%(interactive)s)
               ), 0) AS batchable_sync_calls,
               SUM(cost_usd / 2) FILTER (
                   WHERE NOT batched AND COALESCE(config_name, '') <> ALL(%(interactive)s)
               ) AS unrealized_savings_usd,
               -- Decided verdicts carrying no tokens: the answer came from a
               -- sibling row's call, so their cost lives there.
               SUM(joint_call_rows) AS joint_call_rows
        FROM {ledger_rollup.groups(_WINDOW_START, where="arm = 'answer' AND model IS NOT NULL")} a
        GROUP BY 1, 2, 3
        """,
        params,
    )

    def folded(rows: list[dict[str, Any]], **keys: Any) -> dict[str, Any]:
        acc = grouped.fold(rows, _VERDICT_SUMS, _VERDICT_COUNTS, ("first_call",), ("last_call",))
        for k in _VERDICT_SUMS:
            if k != "cache_write_tokens" and acc[k] is None:
                acc[k] = 0
        return acc | keys

    whole = folded(groups)
    # Ties in cost had no order in SQL (ORDER BY cost alone); the key breaks
    # them now so the order is at least stable.
    check_types = sorted(
        (folded(rows, check_type=k) for k, rows in grouped.by(groups, "check_type").items()),
        key=lambda a: (-a["cost_usd"], a["check_type"] is None, a["check_type"] or ""),
    )
    models = sorted(
        (folded(rows, model=k) for k, rows in grouped.by(groups, "model").items()),
        key=lambda a: (-a["cost_usd"], a["model"]),
    )
    days = sorted(
        (folded(rows, day=k) for k, rows in grouped.by(groups, "day").items()),
        key=lambda a: a["day"],
    )
    return (
        grouped.shaped(VerdictTotals, whole),
        grouped.shaped(BatchingDiagnostics, whole),
        [grouped.shaped(VerdictCheckTypeSpend, a) for a in check_types],
        [grouped.shaped(VerdictModelSpend, a) for a in models],
        [grouped.shaped(VerdictDaySpend, a) for a in days],
    )


def verdict_waste(params: dict, answers: str = _ANSWERS, later: str = "ai_queries") -> Waste:
    """Failed calls, and decided verdicts a later decided verdict on the same
    url and check replaced. `later` is where the replacing verdicts are
    looked for: every answer, or (from spend_window) the window's answers,
    which holds every answer newer than the window's start."""
    waste = db.query_one_as(
        Waste,
        f"""
        WITH scoped AS (
            SELECT id, url, check_type, status, cost_usd
            FROM {answers} a
        ),
        superseded AS (
            SELECT s.id, s.cost_usd FROM scoped s
            WHERE s.status IN ('passed', 'rejected')
              AND EXISTS (
                  SELECT 1 FROM {later} later
                  WHERE later.url = s.url AND later.check_type = s.check_type
                    AND later.status IN ('passed', 'rejected') AND later.id > s.id
              )
        )
        SELECT COUNT(*) FILTER (WHERE status = 'failed') AS failed_calls,
               COALESCE(SUM(cost_usd) FILTER (WHERE status = 'failed'), 0) AS failed_cost_usd,
               (SELECT COUNT(*) FROM superseded) AS superseded_verdicts,
               (SELECT COALESCE(SUM(cost_usd), 0) FROM superseded) AS superseded_cost_usd
        FROM scoped
        """,
        params,
    )

    assert waste
    return waste


def verdict_reach(
    params: dict, answers: str = _ANSWERS
) -> tuple[list[VerdictReachSpend], list[VerdictSourceReachSpend]]:
    # Group answers against current `jobs` and subscription eligibility into
    # three buckets: `no_posting` (missing job row, distinct from unwanted),
    # `reachable`, and `unreachable`. Read untyped to fold Decimal costs cleanly.
    # It exists because 21.7M of a 99.7M-token 30-day bill reached boards no user
    # had enabled (measured 2026-09-03). Unpriced calls are counted, never summed
    # as zero.
    reach_rows = db.query(
        f"""
        SELECT COALESCE(j.source, '') AS source,
               CASE WHEN j.url IS NULL THEN 'no_posting'
                    WHEN {AI_ELIGIBLE_JOB.format(job="j")} THEN 'reachable'
                    ELSE 'unreachable' END AS reach,
               COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE a.cost_usd IS NULL) AS unpriced_calls,
               COALESCE(SUM(a.cost_usd), 0) AS cost_usd,
               COALESCE(SUM(a.total_tokens), 0) AS total_tokens
        FROM {answers} a
        LEFT JOIN jobs j ON j.url = a.url
        GROUP BY 1, 2 ORDER BY 6 DESC
        """,
        params,
    )
    # Return only unreachable rows per source; reachable counts live in by_reach.
    by_source_reach = [
        VerdictSourceReachSpend(**row) for row in reach_rows if row["reach"] == "unreachable"
    ]
    rolled: dict[str, dict[str, Any]] = {}
    for row in reach_rows:
        acc = rolled.setdefault(
            row["reach"],
            {
                "reach": row["reach"],
                "calls": 0,
                "unpriced_calls": 0,
                "cost_usd": 0,
                "total_tokens": 0,
            },
        )
        for k in ("calls", "unpriced_calls", "cost_usd", "total_tokens"):
            acc[k] += row[k]
    by_reach = sorted(
        (VerdictReachSpend(**acc) for acc in rolled.values()), key=lambda r: r.calls, reverse=True
    )
    return by_reach, by_source_reach


# Materialize the window's answers once per transaction so downstream cuts scan
# a narrow temp table instead of repeating the ai_queries join (15-33s each on
# production, 2026-10-13). Keep model IS NULL rows here because they still
# supersede older verdicts in verdict_waste.
_WINDOW_TABLE = f"""
    CREATE TEMP TABLE spend_window ON COMMIT DROP AS
    SELECT * FROM {model_calls.answers_with_usage(f"q.{_WINDOW}")} a
"""
WINDOW_ANSWERS = "(SELECT * FROM spend_window WHERE model IS NOT NULL)"


@contextmanager
def window_table(params: dict) -> Iterator[None]:
    """Inside: spend_window holds the window's answers, for
    WINDOW_ANSWERS and verdict_waste's `later`."""
    with db.transaction():
        db.execute(_WINDOW_TABLE, params)
        db.execute("ANALYZE spend_window")
        yield
