"""Recorded usage estimates and separately labelled posting-verdict diagnostics.

This surface was split from admin.py when that module had 1466 lines and sixty
endpoints. Keep the cost populations explicit: verify_new can yield closed and
clearance verdicts from one request, booking usage on the closed row. A
check_type is therefore not a cost centre. `joint_call_rows` makes zero-token
decided rows visible, although zero tokens alone cannot prove sibling billing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api import budget, db, scoping
from api import params as params_
from api.auth import AuthedUser
from api.routers.admin import require_admin
from core.store import AI_ELIGIBLE_JOB

router = APIRouter()

# Legacy batching diagnostics classify contexts, not verified transport history.
INTERACTIVE_CONTEXTS = ("explain", "manual")

_WINDOW = "created_at >= now() - make_interval(days => %(days)s)"


def _scalars(sql: str, params: dict) -> dict[str, Any]:
    row = db.query_one(sql, params)
    return dict(row) if row else {}


def _ledger_breakdowns(params: dict) -> dict[str, Any]:
    # One snapshot keeps the breakdowns reconcilable. Reduce to daily model
    # groups before rolling up: DISTINCT model across raw grouping sets caused
    # a 160 ms ledger query on 99,831 duplicated corpus rows versus 54 ms for the old
    # purpose-only query. Keep that volume in view when changing this plan.
    rows = db.query(
        f"""
        WITH daily_models AS (
            SELECT purpose, model, (created_at AT TIME ZONE 'UTC')::date AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE cost_usd IS NOT NULL) AS priced_calls,
                   COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls,
                   COUNT(*) FILTER (WHERE model IS NULL) AS unknown_model_calls,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                   COUNT(*) FILTER (WHERE batched) AS batched_calls,
                   MIN(created_at) AS first_call,
                   MAX(created_at) AS last_call
            FROM api_usage WHERE {_WINDOW}
            GROUP BY 1, 2, 3
        )
        SELECT purpose, model, day, GROUPING(purpose, model, day) AS grouping,
               COALESCE(SUM(calls), 0)::bigint AS calls,
               COALESCE(SUM(calls), 0)::bigint AS ledger_rows,
               COALESCE(SUM(priced_calls), 0)::bigint AS priced_calls,
               COALESCE(SUM(unpriced_calls), 0)::bigint AS unpriced_calls,
               COALESCE(SUM(unknown_model_calls), 0)::bigint AS unknown_model_calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(batched_calls), 0)::bigint AS batched_calls,
               COUNT(DISTINCT model) AS models,
               MIN(first_call) AS first_call,
               MAX(last_call) AS last_call
        FROM daily_models
        GROUP BY GROUPING SETS ((), (purpose), (model), (day))
        """,
        params,
    )
    result: dict[str, Any] = {
        "source": "api_usage",
        "basis": "recorded_estimate",
        "timezone": "UTC",
        "window_days": params["days"],
        "note": (
            "Costs sum stored estimates for recorded usage, not provider invoices or "
            "proof that every call was recorded. Unpriced rows are excluded from costs "
            "and counted separately. Batched flags are recorded metadata, not verified "
            "historical transport provenance. Counts are ledger rows, which may represent "
            "individual requests or batch aggregates."
        ),
        "by_purpose": [],
        "by_model": [],
        "by_day": [],
    }
    dimensions = {3: "purpose", 5: "model", 6: "day"}
    for row in rows:
        grouping = row.pop("grouping")
        dimension = dimensions.get(grouping)
        for name in ("purpose", "model", "day"):
            if name != dimension:
                row.pop(name)
        if dimension:
            result[f"by_{dimension}"].append(row)
        else:
            result["totals"] = row
    for name in ("purpose", "model"):
        result[f"by_{name}"].sort(key=lambda row: (-row["cost_usd"], row[name] or ""))
    result["by_day"].sort(key=lambda row: row["day"])
    return result


@router.get("/admin/spend")
def spend(
    days: int = Query(30, ge=1, le=365),
    user: AuthedUser = Depends(require_admin),
):
    params = {"days": days, "interactive": list(INTERACTIVE_CONTEXTS)}

    totals = _scalars(
        f"""
        SELECT COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
               MIN(created_at) AS first_call,
               MAX(created_at) AS last_call
        FROM ai_queries WHERE {_WINDOW} AND model IS NOT NULL
        """,
        params,
    )

    # Retained compatibility estimate; missing batch IDs do not prove sync use.
    batching = _scalars(
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
        FROM ai_queries WHERE {_WINDOW} AND model IS NOT NULL
        """,
        params,
    )

    by_check_type = db.query(
        f"""
        SELECT check_type,
               COUNT(*) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COUNT(*) FILTER (WHERE batch_id IS NOT NULL) AS batched_calls,
               -- Decided verdicts carrying no tokens: the answer came from a
               -- sibling row's call, so their cost lives there.
               COUNT(*) FILTER (
                   WHERE COALESCE(total_tokens, 0) = 0
                     AND status IN ('passed', 'rejected')
               ) AS joint_call_rows
        FROM ai_queries WHERE {_WINDOW} AND model IS NOT NULL
        GROUP BY check_type ORDER BY 3 DESC
        """,
        params,
    )

    by_model = db.query(
        f"""
        SELECT model, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
        FROM ai_queries WHERE {_WINDOW} AND model IS NOT NULL
        GROUP BY model ORDER BY 3 DESC
        """,
        params,
    )

    by_day = db.query(
        f"""
        SELECT created_at::date AS day,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(cost_usd) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_cost_usd,
               COALESCE(SUM(cost_usd) FILTER (WHERE batch_id IS NULL), 0) AS sync_cost_usd,
               COUNT(*) AS calls
        FROM ai_queries WHERE {_WINDOW} AND model IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        params,
    )

    # Supersession is a diagnostic, not proof that earlier work was unnecessary.
    waste = _scalars(
        f"""
        WITH scoped AS (
            SELECT id, url, check_type, status, cost_usd
            FROM ai_queries WHERE {_WINDOW} AND model IS NOT NULL
        ),
        superseded AS (
            SELECT s.id, s.cost_usd FROM scoped s
            WHERE s.status IN ('passed', 'rejected')
              AND EXISTS (
                  SELECT 1 FROM ai_queries later
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
    by_source_reach = db.query(
        f"""
        SELECT COALESCE(j.source, '') AS source,
               CASE WHEN j.url IS NULL THEN 'no_posting'
                    WHEN {AI_ELIGIBLE_JOB.format(job="j")} THEN 'reachable'
                    ELSE 'unreachable' END AS reach,
               COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE a.cost_usd IS NULL) AS unpriced_calls,
               COALESCE(SUM(a.cost_usd), 0) AS cost_usd,
               COALESCE(SUM(a.total_tokens), 0) AS total_tokens
        FROM ai_queries a
        LEFT JOIN jobs j ON j.url = a.url
        WHERE a.{_WINDOW} AND a.model IS NOT NULL
        GROUP BY 1, 2 ORDER BY 6 DESC
        """,
        params,
    )
    by_reach: dict[str, dict[str, Any]] = {}
    for row in by_source_reach:
        acc = by_reach.setdefault(
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

    # Why the usage ledger must be separate from the verdict log:
    # ai_queries is URL-keyed and cannot represent non-posting work. The
    # original investigation found $18.49 in mail classification spend with
    # no verdict rows, then the largest reported line item. Preserve that
    # measurement as the reason for this separate population, not as a current
    # invoice total or a claim that every paid request is now recorded.
    # Fleet batch hooks and user-call writers record api_usage by purpose;
    # their rows may represent different request counts. New call paths must
    # still be audited for ledger coverage, including failed paid responses.
    ledger = _ledger_breakdowns(params)
    by_purpose = ledger["by_purpose"]
    batching["basis"] = "verdict_metadata_hypothesis"
    batching["note"] = (
        "Missing batch IDs do not establish synchronous transport. "
        "unrealized_savings_usd is a legacy half-cost scenario, not verified savings."
    )
    diagnostics = {
        "source": "ai_queries",
        "basis": "recorded_verdict_estimate",
        "note": (
            "Rows are posting verdicts, not unique provider calls. Joint-call rows, "
            "unknown historical transport and current reach limit interpretation; "
            "superseded verdicts do not prove wasted spend."
        ),
        "totals": totals,
        "batching": batching,
        "by_check_type": by_check_type,
        "by_reach": sorted(by_reach.values(), key=lambda r: r["calls"], reverse=True),
        "by_source_reach": by_source_reach,
        "by_model": by_model,
        "by_day": by_day,
        "waste": waste,
    }

    return {
        "window": {"days": days, "from": totals.get("first_call"), "to": totals.get("last_call")},
        "totals": totals,
        "by_purpose": by_purpose,
        # Expose the ceiling beside recorded spend: when the ceiling lived
        # only inside enforcement, users first discovered it when scheduled
        # work stopped. The two source populations still answer different
        # questions and must remain labelled rather than forced to agree.
        "fleet_budget": budget.fleet_budget_status(),
        "ledger": {
            **ledger,
            "spend_total_usd": ledger["totals"]["cost_usd"],
            "verdict_total_usd": totals.get("cost_usd"),
        },
        "verdict_diagnostics": diagnostics,
        "batching": batching,
        "by_check_type": by_check_type,
        "by_reach": sorted(by_reach.values(), key=lambda r: r["calls"], reverse=True),
        "by_source_reach": by_source_reach,
        "by_model": by_model,
        "by_day": by_day,
        "waste": waste,
        # Named so the client never has to hardcode which contexts are exempt
        # from the batching expectation.
        "interactive_contexts": list(INTERACTIVE_CONTEXTS),
    }


@router.get("/admin/spend/calls")
def spend_calls(
    purpose: str | None = Query(default=None),
    model: str | None = Query(default=None),
    batched: bool | None = Query(default=None),
    unpriced: bool | None = Query(default=None),
    users: str | None = Query(default=None, alias="user"),
    days: int = Query(default=30, ge=1, le=3650),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_admin),
):
    """Recorded usage rows; null cost means unknown price, not a free call."""
    where = ["created_at >= now() - make_interval(days => %(days)s)"]
    params: dict[str, Any] = {"days": days, "limit": limit, "offset": offset}
    if purpose:
        where.append("purpose = %(purpose)s")
        params["purpose"] = purpose
    if model:
        where.append("model = %(model)s")
        params["model"] = model
    if batched is not None:
        where.append("batched = %(batched)s")
        params["batched"] = batched
    if unpriced is not None:
        where.append("cost_usd IS NULL" if unpriced else "cost_usd IS NOT NULL")
    ids = scoping.user_ids(users)
    if ids:
        where.append(scoping.column("user_id"))
        params["user_ids"] = ids
    predicate = " AND ".join(where)

    totals = _scalars(
        f"""
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens
        FROM api_usage WHERE {predicate}
        """,
        params,
    )
    return {
        "calls": db.query(
            f"""
            SELECT id, created_at, purpose, model, key_source, batched,
                   prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                   cost_usd, user_id
            FROM api_usage WHERE {predicate}
            ORDER BY created_at DESC, id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        ),
        "totals": totals,
        "window_days": days,
        "filters": params_.applied(
            purpose=params_.csv(purpose), model=params_.csv(model), user=scoping.echo(ids)
        ),
        "filterable": ["purpose", "model", "batched", "unpriced", "user"],
    }
