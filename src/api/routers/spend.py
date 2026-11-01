"""Admin spend routes for recorded calls, verdict diagnostics, provider costs and
balances.

A verification request can produce several verdicts. The recorded call ledger
and posting verdict diagnostics remain separate populations in the response.
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
from api.spend import balance
from api.spend.ledger import (
    LedgerPurpose,
    LedgerWithTotals,
    read_ledger_breakdowns,
)
from api.spend.verdicts import (
    WINDOW_ANSWERS,
    BatchingDiagnostics,
    VerdictCheckTypeSpend,
    VerdictDaySpend,
    VerdictDiagnostics,
    VerdictModelSpend,
    VerdictReachSpend,
    VerdictSourceReachSpend,
    VerdictTotals,
    Waste,
    read_verdict_breakdowns,
    verdict_reach,
    verdict_waste,
    window_table,
)
from api.spend.window import INTERACTIVE_CONTEXTS
from api.stale_cache import StaleByKey

router = APIRouter()


class SpendWindow(BaseModel):
    """What the figures cover: the requested window, and the first and last
    call actually inside it."""

    days: int
    # `from` is the key on the wire and a keyword in Python, so this one field
    # is built by alias.
    from_: datetime.datetime | None = Field(alias="from")
    to: datetime.datetime | None


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
    """Served stale for admin_stats_cache_seconds per window: the cuts read
    every answer in it and took 69 s on production (2026-10-13)."""
    return _spend_pages.get(days, lambda: _spend(days))


_spend_pages: StaleByKey[int, Spend] = StaleByKey(
    lambda: int(db.get_config("admin_stats_cache_seconds"))
)


def _spend(days: int) -> Spend:
    params = {"days": days, "interactive": list(INTERACTIVE_CONTEXTS)}

    with window_table(params):
        totals, batching, by_check_type, by_model, by_day = read_verdict_breakdowns(params)
        waste = verdict_waste(params, WINDOW_ANSWERS, later="spend_window")
        by_reach, by_source_reach = verdict_reach(params, WINDOW_ANSWERS)

    # Why the usage ledger must be separate from the verdict log:
    # ai_queries is URL-keyed and cannot represent non-posting work. The
    # original investigation found $18.49 in mail classification spend with
    # no verdict rows, then the largest reported line item. Preserve that
    # measurement as the reason for this separate population, not as a current
    # invoice total or a claim that every paid request is now recorded.
    # The ledger is model_calls: one row per paid request, written by
    # api.model_calls whichever path made the call, failed responses included.
    # A new call path must still reach one of its writers.
    ledger = read_ledger_breakdowns(params)
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
    # Null: the call's record did not say whose key (live filter answers
    # before 2026-09-13).
    key_source: str | None
    batched: bool
    # More than one only on a row standing for a batch that kept no
    # per-request record.
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    cache_write_tokens: int | None
    cost_usd: float | None
    user_id: int | None


class CallTotals(BaseModel):
    calls: int
    cost_usd: float
    unpriced_calls: int
    prompt_tokens: int
    completion_tokens: int
    cache_write_tokens: int | None
    cache_write_unknown_calls: int


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
    """Recorded calls; null cost means unknown price, not a free call."""
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
        SELECT COALESCE(SUM(requests), 0) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COALESCE(SUM(requests) FILTER (WHERE cost_usd IS NULL), 0) AS unpriced_calls,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               COALESCE(SUM(requests) FILTER (WHERE cache_write_tokens IS NULL), 0)
                   AS cache_write_unknown_calls
        FROM model_calls WHERE {predicate}
        """,
        params,
    )
    # An aggregate with no GROUP BY returns its row even when nothing matched.
    assert totals
    return SpendCalls(
        calls=db.query_as(
            UsageCall,
            f"""
            SELECT id, created_at, purpose, model, key_source, batched, requests,
                   prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                   cache_write_tokens,
                   cost_usd, user_id
            FROM model_calls WHERE {predicate}
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
    """Provider-reported costs for completed UTC days, separate from usage estimates.

    Served stale for admin_stats_cache_seconds per window and UTC day: the
    provider's API took 1.2 to 1.5 s a read on production (2026-11-01), the
    longest wait on the spend page. Keyed on the day because a new day moves
    the window. A read that is not available is answered, never cached, so
    the next visit asks the provider again."""
    today = datetime.datetime.now(datetime.UTC).date()
    try:
        return _provider_pages.get((days, today), lambda: _available_costs(days))
    except _ProviderUnavailable as unavailable:
        return unavailable.costs


_provider_pages: StaleByKey[tuple[int, datetime.date], ProviderCosts] = StaleByKey(
    lambda: int(db.get_config("admin_stats_cache_seconds"))
)


class _ProviderUnavailable(Exception):
    def __init__(self, costs: ProviderCosts) -> None:
        self.costs = costs


def _available_costs(days: int) -> ProviderCosts:
    from core.provider_costs import fetch_costs

    costs = ProviderCosts(**fetch_costs(days))
    if costs.status != "available":
        raise _ProviderUnavailable(costs)
    return costs


class ProviderSpendItem(BaseModel):
    item: str
    amount_usd: float


class ProviderSpendDay(BaseModel):
    day: datetime.date
    total_usd: float


class ProviderSpend(BaseModel):
    """Provider-reported spend over completed UTC days."""

    yesterday_usd: float
    last_7_days_usd: float
    last_30_days_usd: float
    month_to_date_usd: float
    daily_rate_usd: float
    item_basis: str
    by_item: list[ProviderSpendItem]
    daily: list[ProviderSpendDay]


class ProviderLimit(BaseModel):
    """OpenAI's hard spend limit, as the provider reports it."""

    status: str
    reason: str | None
    threshold_usd: float | None
    interval: str | None
    enforcement: str | None


class ProviderHeadroom(BaseModel):
    """Money left on one basis. Always derived, never provider-reported:
    `derivation` names how, and `unavailable` says why a basis that applies
    has no figure."""

    basis: str
    label: str | None = None
    derivation: str | None = None
    amount_usd: float | None
    spent_usd: float | None = None
    granted_usd: float | None = None
    granted_on: datetime.date | None = None
    limit_usd: float | None = None
    runway_days: float | None = None
    ends_on: datetime.date | None = None
    unavailable: str | None
    alert: dict[str, str] | None


class ProviderBalance(BaseModel):
    provider: str
    status: str
    reason: str | None
    scope: str
    fetched_at: str
    spend: ProviderSpend | None
    limit: ProviderLimit | None
    headrooms: list[ProviderHeadroom]


class ProviderBalances(BaseModel):
    providers: list[ProviderBalance]
    alert_usd: int
    alert_days: int


@router.get("/admin/spend/balance")
def provider_balances(user: AuthedUser = Depends(require_admin)) -> ProviderBalances:
    """Each provider's reported spend and its derived money left.

    The provider reads are served stale like /admin/spend/provider, keyed on
    the UTC day and the read window; an unreadable read is answered and never
    cached. The derivation reads the admin's settings on every request, so a
    changed grant or threshold shows at once."""
    today = datetime.datetime.now(datetime.UTC).date()
    providers = []
    for provider in balance.PROVIDERS:
        granted_on = (balance.prepaid_for(provider) or {}).get("granted_on")
        try:
            raw = _balance_reads.get(
                (provider, granted_on, today), lambda p=provider, g=granted_on: _readable(p, g)
            )
        except _BalanceUnreadable as unreadable:
            raw = unreadable.raw
        providers.append(ProviderBalance(**balance.report(provider, raw)))
    alert_usd, alert_days = balance.thresholds()
    return ProviderBalances(providers=providers, alert_usd=alert_usd, alert_days=alert_days)


_balance_reads: StaleByKey[tuple[str, str | None, datetime.date], dict[str, Any]] = StaleByKey(
    lambda: int(db.get_config("admin_stats_cache_seconds"))
)


class _BalanceUnreadable(Exception):
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw


def _readable(provider: str, granted_on: str | None) -> dict[str, Any]:
    raw = balance.read(provider, granted_on)
    if not balance.readable(raw):
        raise _BalanceUnreadable(raw)
    return raw
