"""Decision coverage and funnels, separate from the provider invoice ledger."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import db, pagination, params
from api.auth import AuthedUser
from api.review_gate_reads import ReviewDecisions, read_decisions
from api.routers.admin.shared import require_admin

router = APIRouter()


def selection(
    *,
    url: str | None = None,
    prompt_hash: str | None = None,
    stage: str | None = None,
    mode: str | None = None,
    action: str | None = None,
    user: str | None = None,
    managed_board_id: int | None = None,
) -> tuple[str, dict, dict[str, list[str]]]:
    clauses, values, filters = [], {}, {}
    for key, value in {
        "url": url,
        "prompt_hash": prompt_hash,
        "stage": stage,
        "mode": mode,
        "action": action,
    }.items():
        if value is not None:
            clauses.append(f"d.{key}=%({key})s")
            values[key] = value
            filters[key] = [value]
    users = [int(value) for value in params.csv(user) if value.isdigit()]
    if users:
        clauses.append("d.user_id=ANY(%(users)s)")
        values["users"] = users
        filters["user"] = [str(value) for value in users]
    if managed_board_id is not None:
        clauses.append("d.managed_board_id=%(managed_board_id)s")
        values["managed_board_id"] = managed_board_id
        filters["managed_board_id"] = [str(managed_board_id)]
    return " AND ".join(clauses) or "TRUE", values, filters


@router.get("/review-gates/decisions")
def decisions(
    url: str | None = None,
    prompt_hash: str | None = None,
    stage: str | None = None,
    mode: str | None = None,
    action: str | None = None,
    user: str | None = None,
    managed_board_id: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    window_start: datetime.datetime | None = None,
    window_end: datetime.datetime | None = None,
    admin: AuthedUser = Depends(require_admin),
) -> ReviewDecisions:
    where, values, filters = selection(
        url=url,
        prompt_hash=prompt_hash,
        stage=stage,
        mode=mode,
        action=action,
        user=user,
        managed_board_id=managed_board_id,
    )
    if (window_start is None) != (window_end is None) or (
        window_start is not None
        and window_end is not None
        and (
            window_start.tzinfo is None
            or window_end.tzinfo is None
            or window_start >= window_end
            or window_end - window_start > datetime.timedelta(days=90)
        )
    ):
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_WINDOW",
                "message": "Supply both timezone-aware bounds, increasing and at most 90 days apart.",
            },
        )
    if window_start is not None and window_end is not None:
        where += " AND d.created_at >= %(start)s AND d.created_at < %(end)s"
        values.update(start=window_start, end=window_end)
        filters.update(window_start=[window_start.isoformat()], window_end=[window_end.isoformat()])
    return read_decisions(
        where, values, pagination.Page.from_params(page, page_size, maximum=100), filters
    )


class GateFunnelRow(BaseModel):
    stage: str
    mode: str
    action: str
    decisions: int
    distinct_jobs: int
    recorded_outcomes: int
    unpriced_outcomes: int
    without_recorded_outcome: int
    known_cost_usd: float
    actual_cost_usd: float | None
    agreed_reject: int
    false_reject: int
    unresolved: int


class AvoidedCostEstimate(BaseModel):
    estimated_avoided_cost_usd: float | None
    estimated_decisions: int
    unestimated_decisions: int
    reference_outcomes: int
    basis: str = (
        "Skipped decisions times mean recorded cost per reviewed decision, including retries, in the same decision window, "
        "prompt revision, planned model and transport. A workload-dependent estimate, "
        "not billed savings or a controlled counterfactual."
    )


class GateReport(BaseModel):
    generated_at: datetime.datetime
    window_start: datetime.datetime
    window_end: datetime.datetime
    days: int
    population: str = (
        "Review decisions made during the window, not unique postings or provider calls."
    )
    coverage: str = "Durable decisions only; earlier task-only history is unavailable."
    first_recorded_at: datetime.datetime | None
    rows: list[GateFunnelRow]
    filters: dict[str, list[str]]
    filterable: list[str] = ["prompt_hash", "user", "managed_board_id"]
    avoided_cost: AvoidedCostEstimate
    actual_cost_basis: str = (
        "Stored costs of linked review outcomes for this decision cohort, including retries. "
        "Not total platform spend or the provider invoice. Missing outcomes are not free reviews."
    )


@router.get("/review-gates/report")
def report(
    days: int = Query(7, ge=1, le=90),
    prompt_hash: str | None = None,
    user: str | None = None,
    managed_board_id: int | None = Query(None, ge=1),
    admin: AuthedUser = Depends(require_admin),
) -> GateReport:
    end = datetime.datetime.now(datetime.UTC)
    start = end - datetime.timedelta(days=days)
    where, values, filters = selection(
        prompt_hash=prompt_hash, user=user, managed_board_id=managed_board_id
    )
    first = db.query_one(
        f"SELECT min(d.created_at) AS first FROM review_gate_decisions d WHERE {where}", values
    )
    cohort = (
        "SELECT d.id,d.url,d.stage,d.mode,d.action,d.prompt_hash,"
        "d.evidence->>'planned_model' planned_model,d.evidence->>'transport' transport "
        f"FROM review_gate_decisions d WHERE {where} "
        "AND d.created_at >= %(start)s AND d.created_at < %(end)s"
    )
    bounded = {**values, "start": start, "end": end}
    rows = db.query_as(
        GateFunnelRow,
        f"WITH cohort AS MATERIALIZED ({cohort}), paid AS ("
        "SELECT o.decision_id,count(*) n,count(*) FILTER(WHERE o.recorded_cost_usd IS NULL) unknown, "
        "sum(o.recorded_cost_usd) cost, "
        "count(*) FILTER(WHERE o.rejected IS TRUE) rejected, "
        "count(*) FILTER(WHERE o.rejected IS FALSE) passed, "
        "count(*) FILTER(WHERE o.rejected IS NULL) unresolved "
        "FROM review_gate_outcomes o JOIN cohort c ON c.id=o.decision_id GROUP BY o.decision_id) "
        "SELECT d.stage,d.mode,d.action,count(*) AS decisions,count(DISTINCT d.url) AS distinct_jobs, "
        "COALESCE(sum(p.n),0)::bigint recorded_outcomes, "
        "COALESCE(sum(p.unknown),0)::bigint unpriced_outcomes, "
        "count(*) FILTER(WHERE d.action='review' AND p.decision_id IS NULL) without_recorded_outcome, "
        "COALESCE(sum(p.cost),0)::float known_cost_usd, "
        "CASE WHEN COALESCE(sum(p.unknown),0)=0 AND count(*) FILTER(WHERE d.action='review' "
        "AND p.decision_id IS NULL)=0 THEN COALESCE(sum(p.cost),0)::float END actual_cost_usd, "
        "COALESCE(sum(p.rejected) FILTER(WHERE d.mode='shadow' AND d.stage<>'detailed'),0)::bigint agreed_reject, "
        "COALESCE(sum(p.passed) FILTER(WHERE d.mode='shadow' AND d.stage<>'detailed'),0)::bigint false_reject, "
        "COALESCE(sum(p.unresolved) FILTER(WHERE d.mode='shadow' AND d.stage<>'detailed'),0)::bigint unresolved "
        "FROM cohort d LEFT JOIN paid p ON p.decision_id=d.id "
        "GROUP BY d.stage,d.mode,d.action ORDER BY d.stage,d.mode,d.action",
        bounded,
    )
    estimate = db.query_one(
        f"WITH cohort AS MATERIALIZED ({cohort}), per_decision AS ("
        "SELECT d.id,d.prompt_hash,d.planned_model model,d.transport, "
        "sum(o.recorded_cost_usd)::float cost,count(o.id) n, "
        "count(*) FILTER(WHERE o.recorded_cost_usd IS NULL OR o.model IS DISTINCT FROM "
        "d.planned_model) unknown "
        "FROM cohort d LEFT JOIN review_gate_outcomes o ON o.decision_id=d.id "
        "WHERE d.action='review' GROUP BY d.id,d.prompt_hash,d.planned_model,d.transport), baseline AS ("
        "SELECT prompt_hash,model,transport,avg(cost)::float mean_cost,sum(n) n "
        "FROM per_decision GROUP BY prompt_hash,model,transport "
        "HAVING sum(unknown)=0), skipped AS ("
        "SELECT d.prompt_hash,d.planned_model model,d.transport,count(*) n "
        "FROM cohort d WHERE d.action='skip' GROUP BY d.prompt_hash,d.planned_model,d.transport) "
        "SELECT CASE WHEN count(r.mean_cost)>0 THEN sum(s.n*r.mean_cost)::float END estimated_avoided_cost_usd, "
        "COALESCE(sum(s.n) FILTER(WHERE r.mean_cost IS NOT NULL),0)::bigint estimated_decisions, "
        "COALESCE(sum(s.n) FILTER(WHERE r.mean_cost IS NULL),0)::bigint unestimated_decisions, "
        "COALESCE(sum(r.n),0)::bigint reference_outcomes "
        "FROM skipped s LEFT JOIN baseline r USING(prompt_hash,model,transport)",
        bounded,
    )
    assert estimate is not None
    return GateReport(
        generated_at=end,
        window_start=start,
        window_end=end,
        days=days,
        first_recorded_at=first["first"] if first else None,
        rows=rows,
        filters=filters,
        avoided_cost=AvoidedCostEstimate.model_validate(estimate),
    )
