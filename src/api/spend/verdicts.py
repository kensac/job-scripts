"""Posting-verdict cost diagnostics, separate from model-call spend."""

import datetime
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel

from api import db, grouped, model_calls
from api.spend.window import _ANSWERS, _WINDOW
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
    answers: str = _ANSWERS,
) -> tuple[
    VerdictTotals,
    BatchingDiagnostics,
    list[VerdictCheckTypeSpend],
    list[VerdictModelSpend],
    list[VerdictDaySpend],
]:
    """Totals, batching and the check type, model and day cuts of the verdict
    log, from ONE scan of its answers (each with its call's usage on the
    first answer naming it) grouped by every dimension they cut by.

    Each used to be its own full scan: five of the eight scans /admin/spend
    ran, which took about 97 s between them on a loaded production host on
    2026-10-04. One pass grouped by a superset of these keys measured
    3.2-4.0 s there (327 groups); the rows returned are the window's distinct
    (day, model, check_type), not its calls. Batching splits are FILTERs
    inside each group rather than keys. Day stays `created_at::date`, so its
    boundary is still the session timezone. Costs fold as the Decimals
    Postgres returned, so a sum of group sums is the number one SUM gave.
    """
    groups = db.query(
        f"""
        SELECT created_at::date AS day, model, check_type,
               COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls,
               SUM(cost_usd) AS cost_usd,
               SUM(prompt_tokens) AS prompt_tokens,
               SUM(completion_tokens) AS completion_tokens,
               SUM(total_tokens) AS total_tokens,
               SUM(cached_tokens) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COUNT(*) FILTER (WHERE cache_write_tokens IS NULL) AS cache_write_unknown_calls,
               SUM(reasoning_tokens) AS reasoning_tokens,
               MIN(created_at) AS first_call,
               MAX(created_at) AS last_call,
               COUNT(*) FILTER (WHERE batch_id IS NOT NULL) AS batched_calls,
               COUNT(*) FILTER (WHERE batch_id IS NULL) AS sync_calls,
               SUM(cost_usd) FILTER (WHERE batch_id IS NOT NULL) AS batched_cost_usd,
               SUM(cost_usd) FILTER (WHERE batch_id IS NULL) AS sync_cost_usd,
               COUNT(*) FILTER (
                   WHERE batch_id IS NULL
                     AND COALESCE(config_name, '') <> ALL(%(interactive)s)
               ) AS batchable_sync_calls,
               SUM(cost_usd / 2) FILTER (
                   WHERE batch_id IS NULL
                     AND COALESCE(config_name, '') <> ALL(%(interactive)s)
               ) AS unrealized_savings_usd,
               -- Decided verdicts carrying no tokens: the answer came from a
               -- sibling row's call, so their cost lives there.
               COUNT(*) FILTER (
                   WHERE COALESCE(total_tokens, 0) = 0
                     AND status IN ('passed', 'rejected')
               ) AS joint_call_rows
        FROM {answers} a
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
    # Why source reach has its own breakdown (historical measurement):
    #
    # The sweeps that spend tokens selected postings with no reference to who
    # subscribes to what, so 21.7M of a 99.7M-token 30-day bill went to boards
    # no user had enabled and to one an admin had switched off. This motivated
    # the AI_ELIGIBLE_JOB gate and retaining a source-reach breakdown,
    # because the only symptom was a number in a bill nobody attributed.
    #
    # Measured 2026-09-03. The ticket's own figure was 31.6%, counting
    # `sheet_import` as unsubscribed; it is reachable, so the honest share is
    # 21.8%.
    #
    # Those figures describe the recorded 2026-09-03 sample, not today's usage
    # or a reconciled provider invoice. This query joins current jobs and
    # subscriptions; it cannot reconstruct reach when an older request ran.
    #
    # Three buckets, not two. 'no_posting' is a call whose url has no jobs row,
    # and the original url-keyed extraction design deliberately reached the
    # fifth of that corpus whose posting row was gone and whose cached page
    # could not be scraped again. That historical population does not establish
    # eligibility in today's sweep; inspect its current candidate query. That work cannot be attributed to a source, which is a different
    # fact from being unwanted - folding it into 'unreachable' would report
    # deliberate work as waste. Unpriced calls are counted, never summed as
    # zero: a NULL cost is a rate nobody looked up, not a free call.
    #
    # Read untyped, because these rows answer two questions: one row per
    # (source, reach), and the reach totals rolled up from them. The rollup
    # adds the costs as the Decimals the database returned rather than as the
    # floats the response carries.
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
    by_source_reach = [VerdictSourceReachSpend(**row) for row in reach_rows]
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


# The window's answers with their usage, read once into a temporary table so
# the cuts above aggregate a narrow table instead of each rebuilding the join
# over ai_queries' wide rows. Each rebuilt it from scratch: 15 s, 19 s and
# 33 s for the 30-day window on production (2026-10-13), and the replaced-
# verdict check scanned every answer again. The model filter is applied when
# read, not here: a verdict with no model still replaces an older one.
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
