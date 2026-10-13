"""The answer ledger summed per UTC hour (answer_usage_hourly), and reads of
it that are exact.

A read of [start, end) takes an hour from the table only when the hour lies
wholly inside the window, is older than ledger_rollup_state.computed_until,
and has no row in ledger_dirty_hours (a write since its computation). Every
other part of the window (the partial hour at its start, everything from the
watermark on, each dirty hour) is aggregated from ai_queries and page_fetches
in the same statement, so one snapshot answers and the refresh changes only
how fast. tasks.ledger_rollup is the refresh.

A row stands for an hour and a combination of DIMENSIONS; a reader groups
the rows by the dimensions it cuts by and adds the MEASURES. Day is
`hour::date` in the session timezone, which is the answer's own
`created_at::date` for any timezone whose offset is whole hours.
ponytail: hourly buckets, so a half-hour timezone's day boundary is off by
the half hour; quarter-hour buckets if one is ever used.
"""

from __future__ import annotations

import datetime
from typing import Any

from api import db

DIMENSIONS = ("arm", "model", "check_type", "status", "config_name", "prompt_hash", "batched")

# Each measure and how a sum of groups adds it up. Every one is a SUM except
# the first and last time an answer was recorded.
MEASURES = {
    "answers": "SUM",
    "unpriced": "SUM",
    "cache_write_unknown": "SUM",
    "joint_call_rows": "SUM",
    "cost_usd": "SUM",
    "prompt_tokens": "SUM",
    "completion_tokens": "SUM",
    "total_tokens": "SUM",
    "cached_tokens": "SUM",
    "cache_write_tokens": "SUM",
    "reasoning_tokens": "SUM",
    "first_at": "MIN",
    "last_at": "MAX",
}

_HOUR = "date_trunc('hour', {col}, 'UTC')"

_USAGE = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")
_USAGE += ("cache_write_tokens", "reasoning_tokens", "cost_usd")


def _usage(column: str) -> str:
    """An answer's share of its call, as model_calls.answers_with_usage gives
    it: the call's numbers on the first answer naming it, zeros on the rest,
    none on an answer naming no call."""
    sibling = "m.cost_usd * 0" if column == "cost_usd" else "0"
    return (
        f"CASE WHEN q.model_call_id IS NULL THEN NULL "
        f"WHEN q.id = c.first_id THEN m.{column} ELSE {sibling} END"
    )


def aggregate(ranges: str) -> str:
    """Hourly groups of every answer and fetch whose created_at falls in one
    of `ranges` (a relation with columns lo, hi), in answer_usage_hourly's
    columns. The refresh stores it; a read adds it to what is stored."""
    usage = ",\n".join(f"{_usage(c)} AS {c}" for c in _USAGE)
    return f"""
        SELECT {_HOUR.format(col="a.created_at")} AS hour,
               a.arm, a.model, a.check_type, a.status, a.config_name, a.prompt_hash,
               a.batched,
               COUNT(*) AS answers,
               COUNT(*) FILTER (WHERE a.cost_usd IS NULL) AS unpriced,
               COUNT(*) FILTER (WHERE a.cache_write_tokens IS NULL) AS cache_write_unknown,
               COUNT(*) FILTER (
                   WHERE COALESCE(a.total_tokens, 0) = 0 AND a.status IN ('passed', 'rejected')
               ) AS joint_call_rows,
               SUM(a.cost_usd) AS cost_usd,
               SUM(a.prompt_tokens) AS prompt_tokens,
               SUM(a.completion_tokens) AS completion_tokens,
               SUM(a.total_tokens) AS total_tokens,
               SUM(a.cached_tokens) AS cached_tokens,
               SUM(a.cache_write_tokens) AS cache_write_tokens,
               SUM(a.reasoning_tokens) AS reasoning_tokens,
               MIN(a.created_at) AS first_at,
               MAX(a.created_at) AS last_at
        FROM (
            SELECT q.created_at, 'answer' AS arm, q.model, q.check_type, q.status,
                   q.config_name, q.prompt_hash, q.batch_id IS NOT NULL AS batched,
                   {usage}
            FROM {ranges} r
            JOIN ai_queries q ON q.created_at >= r.lo AND q.created_at < r.hi
            LEFT JOIN model_calls m ON m.id = q.model_call_id
            LEFT JOIN LATERAL (
                SELECT min(o.id) AS first_id FROM ai_queries o
                WHERE o.model_call_id = q.model_call_id
            ) c ON TRUE
            UNION ALL
            -- ledger_rows' fetch arm: a fetch other than a verification copy,
            -- with no model, no call and so no usage.
            SELECT f.created_at, 'fetch', NULL, 'content', f.status,
                   'content-cache', NULL, FALSE,
                   NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint,
                   NULL::bigint, NULL::bigint, NULL::numeric
            FROM {ranges} r
            JOIN page_fetches f ON f.created_at >= r.lo AND f.created_at < r.hi
            WHERE f.method <> 'verification'
        ) a
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
    """


def groups(
    start: str = "'-infinity'::timestamptz",
    end: str = "'infinity'::timestamptz",
    where: str = "TRUE",
) -> str:
    """A subquery: hourly groups covering [start, end) exactly, the stored ones
    where they can be trusted and the rest aggregated now, with `where` (over
    the DIMENSIONS) applied to both. `start` and `end` are SQL timestamptz
    expressions, e.g. `now() - make_interval(days => %(days)s)`."""
    cols = ", ".join(("hour", *DIMENSIONS, *MEASURES))
    return f"""(
        WITH b AS (
            SELECT s, e, ceil_s,
                   LEAST(
                       COALESCE((SELECT computed_until FROM ledger_rollup_state), '-infinity'),
                       {_HOUR.format(col="e")}
                   ) AS stored_end
            FROM (
                SELECT s, e,
                       CASE WHEN {_HOUR.format(col="s")} = s THEN s
                            ELSE {_HOUR.format(col="s")} + interval '1 hour'
                       END AS ceil_s
                FROM (SELECT ({start})::timestamptz AS s, ({end})::timestamptz AS e) bounds
            ) w
        ), dirty AS (
            SELECT DISTINCT d.hour FROM ledger_dirty_hours d, b
            WHERE d.hour >= b.ceil_s AND d.hour < b.stored_end
        ), live_ranges AS (
            SELECT s AS lo, LEAST(ceil_s, e) AS hi FROM b WHERE s < LEAST(ceil_s, e)
            UNION ALL
            SELECT GREATEST(stored_end, ceil_s), e FROM b WHERE GREATEST(stored_end, ceil_s) < e
            UNION ALL
            SELECT hour, hour + interval '1 hour' FROM dirty
        )
        SELECT {cols} FROM answer_usage_hourly h, b
        WHERE h.hour >= b.ceil_s AND h.hour < b.stored_end
          AND h.hour NOT IN (SELECT hour FROM dirty) AND ({where})
        UNION ALL
        SELECT {cols} FROM ({aggregate("live_ranges")}) live WHERE ({where})
    )"""


def refresh(max_span: datetime.timedelta = datetime.timedelta(days=2)) -> dict[str, Any]:
    """One step: recompute the dirty hours below the watermark, then advance
    it by up to `max_span` toward the last complete hour. Returns what it did;
    `caught_up` is false while history is still being summed. One refresher
    at a time (an advisory lock); a second returns at once."""
    with db.transaction():
        lock = db.query_one("SELECT pg_try_advisory_xact_lock(hashtext('ledger-rollup')) AS got")
        if not (lock and lock["got"]):
            return {"dirty_hours": 0, "advanced_to": None, "caught_up": False, "skipped": True}
        state = db.query_one(
            f"""
            SELECT (SELECT computed_until FROM ledger_rollup_state) AS until,
                   {_HOUR.format(col="now()")} AS target,
                   LEAST(
                       (SELECT {_HOUR.format(col="min(created_at)")} FROM ai_queries),
                       (SELECT {_HOUR.format(col="min(created_at)")} FROM page_fetches)
                   ) AS first_hour
            """
        )
        assert state
        until = state["until"] or state["first_hour"] or state["target"]
        logged = db.query("SELECT id, hour FROM ledger_dirty_hours")
        dirty = sorted({r["hour"] for r in logged if r["hour"] < until})
        if dirty:
            db.execute(
                "DELETE FROM answer_usage_hourly WHERE hour = ANY(%(hours)s)", {"hours": dirty}
            )
            _store(dirty, [h + datetime.timedelta(hours=1) for h in dirty])
        if logged:
            db.execute(
                "DELETE FROM ledger_dirty_hours WHERE id = ANY(%(ids)s)",
                {"ids": [r["id"] for r in logged]},
            )
        target = min(state["target"], until + max_span)
        if until < target:
            db.execute(
                "DELETE FROM answer_usage_hourly WHERE hour >= %(lo)s AND hour < %(hi)s",
                {"lo": until, "hi": target},
            )
            _store([until], [target])
        db.execute(
            "INSERT INTO ledger_rollup_state (id, computed_until) VALUES (1, %(t)s) "
            "ON CONFLICT (id) DO UPDATE SET computed_until = EXCLUDED.computed_until",
            {"t": max(until, target)},
        )
    return {
        "dirty_hours": len(dirty),
        "advanced_to": max(until, target),
        "caught_up": target >= state["target"],
        "skipped": False,
    }


def _store(lo: list[datetime.datetime], hi: list[datetime.datetime]) -> None:
    columns = ", ".join(("hour", *DIMENSIONS, *MEASURES))
    ranges = "(SELECT * FROM unnest(%(lo)s::timestamptz[], %(hi)s::timestamptz[]) r(lo, hi))"
    db.execute(
        f"INSERT INTO answer_usage_hourly ({columns}) SELECT {columns} FROM ({aggregate(ranges)}) g",
        {"lo": lo, "hi": hi},
    )
