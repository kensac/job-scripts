"""Recorded usage estimates and separately labelled posting-verdict diagnostics.

This surface was split from admin.py when that module had 1466 lines and sixty
endpoints. Keep the cost populations explicit: verify_new can yield closed and
clearance verdicts from one request, booking usage on the closed row. A
check_type is therefore not a cost centre. `joint_call_rows` makes zero-token
decided rows visible, although zero tokens alone cannot prove sibling billing.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api import budget, db, scoping
from api import params as params_
from api.auth import AuthedUser
from api.routers.admin import require_admin
from core.store import AI_ELIGIBLE_JOB

router = APIRouter()


# Every money field below is a float and every token count an int, which is
# what a Postgres SUM already became on the way out: a sum arrives as a
# Decimal, and a Decimal reached the wire as a number. Declaring these Decimal
# would send money as a string instead, which is a different answer.

# Legacy batching diagnostics classify contexts, not verified transport history.
INTERACTIVE_CONTEXTS = ("explain", "manual")

_WINDOW = "created_at >= now() - make_interval(days => %(days)s)"


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


def _ledger_breakdowns(params: dict) -> Ledger:
    # One snapshot keeps the breakdowns reconcilable. Reduce to daily model
    # groups before rolling up: DISTINCT model across raw grouping sets caused
    # a 160 ms ledger query on 99,831 duplicated corpus rows versus 54 ms for the old
    # purpose-only query. Keep that volume in view when changing this plan.
    rows = db.query(
        f"""
        WITH daily_models AS (
            SELECT purpose, NULLIF(BTRIM(model), '') AS model,
                   (created_at AT TIME ZONE 'UTC')::date AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE cost_usd IS NOT NULL) AS priced_calls,
                   COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls,
                   COUNT(*) FILTER (WHERE NULLIF(BTRIM(model), '') IS NULL) AS unknown_model_calls,
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
        source="api_usage",
        basis="recorded_estimate",
        timezone="UTC",
        window_days=params["days"],
        note=(
            "Costs sum stored estimates for recorded usage, not provider invoices or "
            "proof that every call was recorded. Unpriced rows are excluded from costs "
            "and counted separately. Batched flags are recorded metadata, not verified "
            "historical transport provenance. Counts are ledger rows, which may represent "
            "individual requests or batch aggregates."
        ),
        totals=totals,
        by_purpose=buckets["purpose"],
        by_model=buckets["model"],
        by_day=buckets["day"],
    )


class SpendWindow(BaseModel):
    """What the figures cover: the requested window, and the first and last
    call actually inside it."""

    days: int
    # `from` is the key on the wire and a keyword in Python, so this one field
    # is built by alias.
    from_: datetime.datetime | None = Field(alias="from")
    to: datetime.datetime | None


class VerdictTotals(BaseModel):
    cost_usd: float
    calls: int
    unpriced_calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
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


class FleetBudget(BaseModel):
    """Where fleet spend sits against its ceiling, with what the ceiling does
    not cover carried as data rather than left in a comment."""

    enabled: bool
    spent_usd: float
    ceiling_usd: float
    cycles: int
    cycle_cost_usd: float
    headroom_usd: float | None
    used_fraction: float | None
    projected_usd: float | None
    projected_exceeds: bool
    scope: str
    excludes: str
    shared_key_possible: bool
    shared_key_note: str


class Spend(BaseModel):
    """Two populations, kept labelled: the usage ledger, and the verdict log.

    Everything under `verdict_diagnostics` is repeated at the top level, which
    is where the clients read it from. Nothing has moved; the nesting names
    the population the numbers come from.
    """

    window: SpendWindow
    totals: VerdictTotals
    by_purpose: list[LedgerPurpose]
    # The ceiling beside recorded spend: when it lived only inside
    # enforcement, users first discovered it when scheduled work stopped.
    fleet_budget: FleetBudget
    ledger: LedgerWithTotals
    verdict_diagnostics: VerdictDiagnostics
    batching: BatchingDiagnostics
    by_check_type: list[VerdictCheckTypeSpend]
    by_reach: list[VerdictReachSpend]
    by_source_reach: list[VerdictSourceReachSpend]
    by_model: list[VerdictModelSpend]
    by_day: list[VerdictDaySpend]
    waste: Waste
    # Named so the client never has to hardcode which contexts are exempt
    # from the batching expectation.
    interactive_contexts: list[str]


@router.get("/admin/spend")
def spend(
    days: int = Query(30, ge=1, le=365),
    user: AuthedUser = Depends(require_admin),
) -> Spend:
    params = {"days": days, "interactive": list(INTERACTIVE_CONTEXTS)}

    totals = db.query_one_as(
        VerdictTotals,
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

    # An aggregate with no GROUP BY returns its row however empty the window.
    assert totals

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
        FROM ai_queries WHERE {_WINDOW} AND model IS NOT NULL
        """,
        params,
    )

    assert batching

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

    by_model = db.query_as(
        VerdictModelSpend,
        f"""
        SELECT model, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
        FROM ai_queries WHERE {_WINDOW} AND model IS NOT NULL
        GROUP BY model ORDER BY 3 DESC
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
        FROM ai_queries WHERE {_WINDOW} AND model IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        params,
    )

    waste = db.query_one_as(
        Waste,
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

    assert waste

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
        FROM ai_queries a
        LEFT JOIN jobs j ON j.url = a.url
        WHERE a.{_WINDOW} AND a.model IS NOT NULL
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
    diagnostics = VerdictDiagnostics(
        source="ai_queries",
        basis="recorded_verdict_estimate",
        note=(
            "Rows are posting verdicts, not unique provider calls. Joint-call rows, "
            "unknown historical transport and current reach limit interpretation; "
            "superseded verdicts do not prove wasted spend."
        ),
        totals=totals,
        batching=batching,
        by_check_type=by_check_type,
        by_reach=by_reach,
        by_source_reach=by_source_reach,
        by_model=by_model,
        by_day=by_day,
        waste=waste,
    )

    return Spend(
        window=SpendWindow.model_validate(
            {"days": days, "from": totals.first_call, "to": totals.last_call}
        ),
        totals=totals,
        by_purpose=ledger.by_purpose,
        fleet_budget=FleetBudget(**budget.fleet_budget_status()),
        ledger=LedgerWithTotals(
            **ledger.model_dump(),
            spend_total_usd=ledger.totals.cost_usd,
            verdict_total_usd=totals.cost_usd,
        ),
        verdict_diagnostics=diagnostics,
        batching=batching,
        by_check_type=by_check_type,
        by_reach=by_reach,
        by_source_reach=by_source_reach,
        by_model=by_model,
        by_day=by_day,
        waste=waste,
        interactive_contexts=list(INTERACTIVE_CONTEXTS),
    )


class UsageCall(BaseModel):
    """One recorded call. A null cost means nobody looked the price up, never
    that the call was free."""

    id: int
    created_at: datetime.datetime
    purpose: str
    model: str | None
    key_source: str
    batched: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    cost_usd: float | None
    user_id: int | None


class CallTotals(BaseModel):
    calls: int
    cost_usd: float
    unpriced_calls: int
    prompt_tokens: int
    completion_tokens: int


class SpendCalls(BaseModel):
    calls: list[UsageCall]
    totals: CallTotals
    window_days: int
    # Only the filters that narrowed anything, so a client can tell "lists
    # accepted" from "one value only" without guessing.
    filters: dict[str, list[str]]
    filterable: list[str]


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
) -> SpendCalls:
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

    totals = db.query_one_as(
        CallTotals,
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
    # An aggregate with no GROUP BY returns its row even when nothing matched.
    assert totals
    return SpendCalls(
        calls=db.query_as(
            UsageCall,
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
        totals=totals,
        window_days=days,
        filters=params_.applied(
            purpose=params_.csv(purpose), model=params_.csv(model), user=scoping.echo(ids)
        ),
        filterable=["purpose", "model", "batched", "unpriced", "user"],
    )


class ProviderCostItem(BaseModel):
    amount_usd: float
    project_id: str | None
    line_item: str | None


class ProviderCostDay(BaseModel):
    start_time: int
    end_time: int
    total_usd: float
    items: list[ProviderCostItem]


class ProviderCosts(BaseModel):
    """What the provider says it charged, as reported now rather than a
    finalized invoice. A window that could not be read whole reports its
    reason and no total; it never reports zero."""

    provider: str
    basis: str
    status: str
    reason: str | None
    scope: str
    project_ids: list[str]
    window_basis: str
    start_time: int | None
    end_time: int
    fetched_at: str
    total_usd: float | None
    daily: list[ProviderCostDay]


@router.get("/admin/spend/provider")
def provider_spend(
    days: int = Query(30, ge=1, le=365),
    user: AuthedUser = Depends(require_admin),
) -> ProviderCosts:
    """Provider-reported costs for completed UTC days, separate from usage estimates."""
    from core.provider_costs import fetch_costs

    return ProviderCosts(**fetch_costs(days))
