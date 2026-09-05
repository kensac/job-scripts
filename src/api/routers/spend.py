"""AI spend: where the money goes, and what it would take to spend less.

Split out of admin.py rather than added to it - that module is already 1466
lines and sixty endpoints.

One thing this surface has to be honest about: a check_type is NOT a cost
centre. verify_new asks one batched question that yields both a closed and a
clearance verdict, and the usage is booked entirely to the closed row so the
call is not counted twice (tasks/verify.py:373). Clearance therefore looks
nearly free. Rather than hide that, every breakdown carries `joint_call_rows`
- decided verdicts whose cost sits on a sibling row - so the number explains
its own shape.
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

# Contexts where a human is waiting on the answer. CLAUDE.md's rule is that
# only these may run synchronously; everything else batches at half price. So
# unbatched spend outside this set is not a fact about the workload, it is
# money left on the table, and the endpoint reports it as such.
INTERACTIVE_CONTEXTS = ("explain", "manual")

_WINDOW = "created_at >= now() - make_interval(days => %(days)s)"


def _scalars(sql: str, params: dict) -> dict[str, Any]:
    row = db.query_one(sql, params)
    return dict(row) if row else {}


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

    # Batch coverage, and the specific dollars that coverage would recover.
    # Halving is exact rather than an estimate: the Batch API bills at half the
    # synchronous rate for identical tokens.
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

    # Spend that bought nothing. Failures are the obvious half; the subtler
    # half is verdicts already superseded by a later one on the same
    # (url, check_type) - work that was paid for and then overwritten.
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

    # What the bill bought that nobody can open.
    #
    # The sweeps that spend tokens selected postings with no reference to who
    # subscribes to what, so 21.7M of a 99.7M-token 30-day bill went to boards
    # no user had enabled and to one an admin had switched off. The gate is
    # fixed (core/store.py AI_ELIGIBLE_JOB); this is how it stays fixed,
    # because the only symptom was a number in a bill nobody attributed.
    #
    # Measured 2026-09-03. The ticket's own figure was 31.6%, counting
    # `sheet_import` as unsubscribed; it is reachable, so the honest share is
    # 21.8%.
    #
    # Three buckets, not two. 'no_posting' is a call whose url has no jobs row,
    # and it is deliberate rather than unexplained: the requirements and
    # embeddings sweeps are url-keyed so they still reach the fifth of the
    # corpus whose posting row is gone and whose page can never be scraped
    # again. That work cannot be attributed to a source, which is a different
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

    # Every AI caller, grouped by the purpose it already declares.
    #
    # The rest of this endpoint reads ai_queries, which is the VERDICT log -
    # URL-keyed, and so structurally blind to any work that is not about a
    # posting. Mail classification is $18.49 of real spend and writes no
    # verdict row, so it was invisible here while being the largest line item
    # in the system.
    #
    # api_usage is the ledger of record for spend and every path writes it: the
    # sync path through record_usage, the batched path through the one hook
    # every task already passes a purpose to. A new caller appears here with no
    # wiring, because it cannot make a batched call without naming a purpose.
    by_purpose = db.query(
        """
        SELECT purpose,
               COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COUNT(*) FILTER (WHERE batched) AS batched_calls,
               COUNT(DISTINCT model) AS models,
               MIN(created_at) AS first_call,
               MAX(created_at) AS last_call
        FROM api_usage
        WHERE created_at >= now() - make_interval(days => %(days)s)
        GROUP BY purpose ORDER BY 4 DESC
        """,
        {"days": days},
    )
    ledger_total = sum((r["cost_usd"] or 0) for r in by_purpose)

    return {
        "window": {"days": days, "from": totals.get("first_call"), "to": totals.get("last_call")},
        "totals": totals,
        "by_purpose": by_purpose,
        # The two ledgers answer different questions and will not agree:
        # ai_queries prices per URL and cannot see non-posting work; api_usage
        # prices every call and cannot say which posting it was about. Stating
        # both, labelled, beats printing one and calling it the total.
        # Where spend sits against its ceiling, not just what it totals. The
        # ceiling existed only inside the check that enforced it, so the first
        # time anyone saw it was when scheduled work stopped - a control nobody
        # can see is a control that only ever surprises.
        "fleet_budget": budget.fleet_budget_status(),
        "ledger": {
            "spend_total_usd": ledger_total,
            "verdict_total_usd": totals.get("cost_usd"),
            "note": (
                "spend_total_usd covers every AI call by purpose; verdict_total_usd "
                "covers only work that produced a posting verdict"
            ),
        },
        "batching": batching,
        "by_check_type": by_check_type,
        # Spend that reached a person, spend that could not, and spend whose
        # posting is unknown - with the sources behind each, so the answer to
        # "why is this not zero" is on the same screen as the number.
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
    """The calls behind a purpose.

    `by_purpose` was a dead end by construction: nothing anywhere renders
    `api_usage` rows, so a purpose's total could be read and never opened. The
    Responses page is over `ai_queries`, which is the verdict log and cannot
    see work that produced no verdict - which is most of the bill.

    `unpriced` is its own filter rather than a cost sort, because a NULL cost
    is not a cheap call. It means nobody looked the rate up, and the set of
    calls we cannot price is a different question from the set that was
    inexpensive.
    """
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
