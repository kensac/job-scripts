"""/admin/spend and /admin/stats each read ai_queries once now, where they
used to scan it once per cut. The cuts must come out exactly as the per-cut
SQL produced them, so the old SQL is kept here as the reference and the two
are compared whole.

The reference ORDER BYs carry one addition: a tie-break on the dimension,
in "C" collation (Python's string order). The old SQL ordered by the measure
alone, so tied rows came out in no defined order; the tie-break picks one of
the orders it was allowed to return, the one the fold now always returns.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from api import db, model_calls
from api.routers.admin import queries
from api.routers.admin.queries import (
    CheckTypeTotals,
    DayTotals,
    LedgerStats,
    LedgerTotals,
    ModelTotals,
    StatusTotals,
)
from api.routers.spend import (
    INTERACTIVE_CONTEXTS,
    BatchingDiagnostics,
    VerdictCheckTypeSpend,
    VerdictDaySpend,
    VerdictModelSpend,
    VerdictTotals,
    _verdict_breakdowns,
)

_WINDOW = "created_at >= now() - make_interval(days => %(days)s)"
# The window's answers with their calls' usage, as the page reads them.
_ANSWERS = model_calls.answers_with_usage(f"q.{_WINDOW} AND q.model IS NOT NULL")


def _old_spend(params: dict) -> tuple[Any, ...]:
    totals = db.query_one_as(
        VerdictTotals,
        f"""
        SELECT COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COUNT(*) FILTER (WHERE cache_write_tokens IS NULL) AS cache_write_unknown_calls,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
               MIN(created_at) AS first_call,
               MAX(created_at) AS last_call
        FROM {_ANSWERS} a
        """,
        params,
    )
    batching = db.query_one_as(
        BatchingDiagnostics,
        f"""
        SELECT COUNT(*) FILTER (WHERE batch_id IS NOT NULL) AS batched_calls,
               COUNT(*) FILTER (WHERE batch_id IS NULL) AS sync_calls,
               COALESCE(SUM(cost_usd) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_cost_usd,
               COALESCE(SUM(cost_usd) FILTER (WHERE batch_id IS NULL), 0) AS sync_cost_usd,
               COUNT(*) FILTER (
                   WHERE batch_id IS NULL
                     AND COALESCE(config_name, '') <> ALL(%(interactive)s)
               ) AS batchable_sync_calls,
               COALESCE(SUM(cost_usd / 2) FILTER (
                   WHERE batch_id IS NULL
                     AND COALESCE(config_name, '') <> ALL(%(interactive)s)
               ), 0) AS unrealized_savings_usd
        FROM {_ANSWERS} a
        """,
        params,
    )
    by_check_type = db.query_as(
        VerdictCheckTypeSpend,
        f"""
        SELECT check_type,
               COUNT(*) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COUNT(*) FILTER (WHERE cache_write_tokens IS NULL) AS cache_write_unknown_calls,
               COUNT(*) FILTER (WHERE batch_id IS NOT NULL) AS batched_calls,
               COUNT(*) FILTER (
                   WHERE COALESCE(total_tokens, 0) = 0
                     AND status IN ('passed', 'rejected')
               ) AS joint_call_rows
        FROM {_ANSWERS} a
        GROUP BY check_type ORDER BY 3 DESC, check_type COLLATE "C" NULLS LAST
        """,
        params,
    )
    by_model = db.query_as(
        VerdictModelSpend,
        f"""
        SELECT model, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
        FROM {_ANSWERS} a
        GROUP BY model ORDER BY 3 DESC, model COLLATE "C"
        """,
        params,
    )
    by_day = db.query_as(
        VerdictDaySpend,
        f"""
        SELECT created_at::date AS day,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(cost_usd) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_cost_usd,
               COALESCE(SUM(cost_usd) FILTER (WHERE batch_id IS NULL), 0) AS sync_cost_usd,
               COUNT(*) AS calls
        FROM {_ANSWERS} a
        GROUP BY 1 ORDER BY 1
        """,
        params,
    )
    return totals, batching, by_check_type, by_model, by_day


def _old_stats() -> LedgerStats:
    totals = db.query_one(
        """
        SELECT COUNT(*) AS queries,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_queries,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COUNT(*) FILTER (WHERE cache_write_tokens IS NULL) AS cache_write_unknown_queries,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
               SUM(cost_usd) AS cost_usd
        FROM ledger_rows
        """
    )
    by_check_type = db.query_as(
        CheckTypeTotals,
        """
        SELECT check_type, COUNT(*) AS count,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens
        FROM ledger_rows GROUP BY check_type
        ORDER BY count DESC, check_type COLLATE "C" NULLS LAST
        """,
    )
    by_status = db.query_as(
        StatusTotals,
        "SELECT status, COUNT(*) AS count FROM ledger_rows GROUP BY status "
        'ORDER BY count DESC, status COLLATE "C" NULLS LAST',
    )
    by_day = db.query_as(
        DayTotals,
        """
        SELECT created_at::date AS day,
               COUNT(*) AS queries,
               COUNT(*) FILTER (WHERE status = 'failed') AS failed,
               COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_queries,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COUNT(*) FILTER (WHERE cache_write_tokens IS NULL) AS cache_write_unknown_queries,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
        FROM ledger_rows GROUP BY day ORDER BY day ASC
        """,
    )
    by_model = db.query(
        """
        SELECT model,
               COUNT(*) AS queries,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_queries,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COUNT(*) FILTER (WHERE cache_write_tokens IS NULL) AS cache_write_unknown_queries,
               COALESCE(SUM(prompt_tokens) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_prompt_tokens,
               COALESCE(SUM(completion_tokens) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_completion_tokens,
               COALESCE(SUM(cached_tokens) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_cached_tokens,
               SUM(cost_usd) AS cost_usd
        FROM ledger_rows WHERE model IS NOT NULL
        GROUP BY model ORDER BY queries DESC, model COLLATE "C"
        """
    )
    priced = [
        ModelTotals(
            **{
                **row,
                "cost_usd": round(float(row["cost_usd"]), 6)
                if row["cost_usd"] is not None
                else None,
            }
        )
        for row in by_model
    ]
    assert totals is not None
    total_cost = totals.pop("cost_usd") or 0
    return LedgerStats(
        totals=LedgerTotals(**totals, cost_usd=round(float(total_cost), 6)),
        by_check_type=by_check_type,
        by_status=by_status,
        by_day=by_day,
        by_model=priced,
    )


def _dump(value: Any) -> Any:
    """The wire form, so a float that differs in its last bit is a failure."""
    if isinstance(value, list):
        return [row.model_dump_json() for row in value]
    return value.model_dump_json()


def _insert(**row: Any) -> int:
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    got = db.query_one(
        f"INSERT INTO ai_queries ({cols}) VALUES ({marks}) RETURNING id", tuple(row.values())
    )
    assert got
    return got["id"]


@pytest.fixture
def verdict_log():
    """Rows chosen to land on every edge the fold has to reproduce:

    - times either side of midnight in UTC, Los Angeles and Kiritimati
      (UTC+14), so a day bucket computed in the wrong zone moves rows;
    - NULL model (excluded from spend, kept by stats), NULL check_type,
      NULL status, NULL cost (unpriced, never zero), NULL cache_write_tokens
      on every row of one group (the sum must stay NULL) and on some rows of
      another (the sum skips them);
    - batched and live rows, interactive and scheduled config names, and a
      NULL config_name (COALESCE'd to '' and so batchable);
    - decided rows with zero and NULL tokens (joint-call rows);
    - one group whose only cost is NULL, so its stats cost is None;
    - rows outside the 30-day window, and rows inserted then deleted.
    """
    base = db.query_one("SELECT date_trunc('day', now() AT TIME ZONE 'UTC') AS d")
    assert base
    edges = ["-10:05", "-07:55", "-08:05", "-00:05", "00:05", "09:55", "10:05", "13:55", "14:05"]
    models = ["gpt-5-nano", "gpt-5-mini", "claude-x", None]
    check_types = ["closed", "clearance", "custom", "extract", None]
    statuses = ["passed", "rejected", "failed", "passed", "error", None]
    configs = ["explain", "manual", "sweep", None, "verify_new"]
    n = 0
    for day_back in (1, 2, 3, 5, 12, 29, 31, 40):
        for edge in edges:
            n += 1
            sign = -1 if edge.startswith("-") else 1
            hh, mm = edge.lstrip("-").split(":")
            model = models[n % len(models)]
            prompt = None if n % 11 == 0 else n * 1000 + 7
            completion = 0 if n % 9 == 0 else (None if n % 13 == 0 else n * 37)
            total = (
                0 if n % 7 == 0 else (None if n % 10 == 0 else (prompt or 0) + (completion or 0))
            )
            # Each answer's usage is its call's, as the writers record it.
            call = db.query_one(
                "INSERT INTO model_calls (purpose, model, batched, prompt_tokens, "
                "completion_tokens, total_tokens, cached_tokens, cache_write_tokens, "
                "reasoning_tokens, cost_usd) VALUES ('verify', %s, false, %s, %s, %s, %s, "
                "%s, %s, %s) RETURNING id",
                (
                    model,
                    prompt or 0,
                    completion or 0,
                    total or 0,
                    0 if n % 6 == 0 else n * 3,
                    None if model == "claude-x" or n % 4 == 0 else n * 2,
                    None if n % 5 == 0 else n,
                    # Six-decimal costs, many with an odd last digit, so cost / 2
                    # needs a seventh place and SUM(cost / 2) is exercised exactly.
                    None if n % 8 == 0 or model == "claude-x" else Decimal(n) / 997,
                ),
            )
            _insert(
                created_at=f"{base['d'].isoformat()}+00:00",
                model=model,
                check_type=check_types[(n * 3) % len(check_types)],
                status=statuses[(n * 5) % len(statuses)],
                config_name=configs[(n * 7) % len(configs)],
                batch_id=f"b-{n}" if n % 3 == 0 else None,
                model_call_id=call["id"],
            )
            db.execute(
                "UPDATE ai_queries SET created_at = created_at "
                "- make_interval(days => %s) + %s * make_interval(hours => %s, mins => %s) "
                "WHERE id = (SELECT max(id) FROM ai_queries)",
                (day_back, sign, int(hh), int(mm)),
            )
    for _ in range(3):
        row_id = _insert(model="gpt-5-nano", check_type="closed", status="passed")
        db.execute("DELETE FROM ai_queries WHERE id = %s", (row_id,))
    # Every interesting population must be present, or the comparison below
    # proves nothing about it.
    shape = db.query_one(
        """
        SELECT COUNT(*) FILTER (WHERE model IS NULL) AS no_model,
               COUNT(*) FILTER (WHERE check_type IS NULL) AS no_check,
               COUNT(*) FILTER (WHERE status IS NULL) AS no_status,
               COUNT(*) FILTER (WHERE cost_usd IS NULL AND model IS NOT NULL) AS unpriced,
               COUNT(*) FILTER (WHERE batch_id IS NOT NULL) AS batched,
               COUNT(*) FILTER (WHERE config_name IS NULL) AS no_config,
               COUNT(*) FILTER (WHERE COALESCE(total_tokens, 0) = 0
                                AND status IN ('passed', 'rejected')) AS joint,
               COUNT(*) FILTER (WHERE created_at < now() - interval '30 days') AS outside
        FROM ledger_rows
        """
    )
    assert shape and all(v > 0 for v in shape.values()), shape


@pytest.mark.parametrize("zone", ["UTC", "America/Los_Angeles", "Pacific/Kiritimati"])
def test_spend_single_pass_matches_the_per_cut_scans(verdict_log, zone):
    params = {"days": 30, "interactive": list(INTERACTIVE_CONTEXTS)}
    with db.transaction():
        db.execute(f"SET LOCAL timezone = '{zone}'")
        old = _old_spend(params)
        new = _verdict_breakdowns(params)
    for before, after in zip(old, new, strict=True):
        assert _dump(after) == _dump(before)
    # A NULL check_type is its own bucket, and the days really straddle.
    assert None in [r.check_type for r in new[2]]
    assert len(new[4]) >= 6


@pytest.mark.parametrize("zone", ["UTC", "America/Los_Angeles", "Pacific/Kiritimati"])
def test_stats_single_pass_matches_the_per_cut_scans(verdict_log, zone):
    with db.transaction():
        db.execute(f"SET LOCAL timezone = '{zone}'")
        old = _old_stats()
        new = queries._compute_stats()
    assert new.model_dump_json() == old.model_dump_json()
    assert [r.cost_usd for r in new.by_model if r.model == "claude-x"] == [None]
    assert None in [r.status for r in new.by_status]


def test_stats_and_spend_on_an_empty_table():
    params = {"days": 30, "interactive": list(INTERACTIVE_CONTEXTS)}
    assert queries._compute_stats().model_dump_json() == _old_stats().model_dump_json()
    old, new = _old_spend(params), _verdict_breakdowns(params)
    assert [_dump(v) for v in new] == [_dump(v) for v in old]
    assert new[2:] == ([], [], [])
