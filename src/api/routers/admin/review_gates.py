"""Decision coverage and funnels, separate from the provider invoice ledger."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db, pagination, scoping
from api.auth import AuthedUser
from api.problem import refuse
from api.review_decision_storage import URL_MATCH
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
    filter_id: int | None = None,
    stored: bool = False,
) -> tuple[str, dict, dict[str, list[str]]]:
    """WHERE over alias d: DECISIONS, or with `stored` the bare review_gate_decisions row."""
    clauses, values, filters = [], {}, {}
    for key, value in {
        "url": url,
        "prompt_hash": prompt_hash,
        "stage": stage,
        "mode": mode,
        "action": action,
    }.items():
        if value is not None:
            if key == "url":
                clauses.append(URL_MATCH)
            elif stored:
                clauses.append(
                    f"d.body_id IN (SELECT id FROM review_gate_decision_bodies WHERE {key}=%({key})s)"
                )
            else:
                clauses.append(f"d.{key}=%({key})s")
            values[key] = value
            filters[key] = [value]
    users = scoping.user_ids(user)
    if users:
        clauses.append("d.user_id=ANY(%(users)s)")
        values["users"] = users
        filters["user"] = [str(value) for value in users]
    if managed_board_id is not None:
        clauses.append("d.managed_board_id=%(managed_board_id)s")
        values["managed_board_id"] = managed_board_id
        filters["managed_board_id"] = [str(managed_board_id)]
    if filter_id is not None:
        clauses.append("d.filter_id=%(filter_id)s AND d.managed_board_id IS NULL")
        values["filter_id"] = filter_id
        filters["filter_id"] = [str(filter_id)]
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
    filter_id: int | None = Query(None, ge=1),
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
        filter_id=filter_id,
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
        raise refuse(
            400,
            "INVALID_WINDOW",
            "Supply both timezone-aware bounds, increasing and at most 90 days apart.",
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
    filterable: list[str] = ["prompt_hash", "user", "managed_board_id", "filter_id"]
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
    filter_id: int | None = Query(None, ge=1),
    admin: AuthedUser = Depends(require_admin),
) -> GateReport:
    return _report(days, prompt_hash, user, managed_board_id, filter_id)


def _report(
    days: int,
    prompt_hash: str | None,
    user: str | None,
    managed_board_id: int | None,
    filter_id: int | None,
) -> GateReport:
    end = datetime.datetime.now(datetime.UTC)
    start = end - datetime.timedelta(days=days)
    # The report reads the stored row, never DECISIONS: url_id and body_id
    # are NOT NULL under validated foreign keys, so no join can add or drop a
    # decision, and each body is resolved once rather than once per decision.
    # Joining all of them to their bodies took 431 s on 7.9M decisions
    # (2026-10-03); min() over the joined view could not use the created_at
    # index and took 61.7 s.
    where, values, filters = selection(
        prompt_hash=prompt_hash,
        user=user,
        managed_board_id=managed_board_id,
        filter_id=filter_id,
        stored=True,
    )
    cohort = (
        "SELECT d.id,d.body_id,d.url_id FROM review_gate_decisions d "
        f"WHERE {where} AND d.created_at >= %(start)s AND d.created_at < %(end)s"
    )
    # One statement, so coverage, funnel and estimate read one snapshot without
    # holding a REPEATABLE READ transaction, and the window is scanned once.
    # Funnel: grouped by (body, URL) before the body is read. Every column is
    # a count or a numeric sum, so it decomposes exactly, and
    # review_gate_urls.url is unique, so distinct url_id counts distinct URLs.
    # Estimate: the mean is over per-decision float costs, as before, so
    # reviews stay one row per decision; skips need only a count per body.
    report = db.query_one(
        f"WITH cohort AS MATERIALIZED ({cohort}), keys AS MATERIALIZED ("
        "SELECT b.id,b.stage,b.mode,b.action,b.prompt_hash,b.evidence->>'planned_model' model, "
        "b.evidence->>'transport' transport FROM review_gate_decision_bodies b "
        "WHERE b.id IN (SELECT body_id FROM cohort)), paid AS ("
        "SELECT o.decision_id,count(*) n,count(*) FILTER(WHERE o.recorded_cost_usd IS NULL) unknown, "
        "sum(o.recorded_cost_usd) cost, "
        "count(*) FILTER(WHERE o.rejected IS TRUE) rejected, "
        "count(*) FILTER(WHERE o.rejected IS FALSE) passed, "
        "count(*) FILTER(WHERE o.rejected IS NULL) unresolved "
        "FROM review_gate_outcomes o JOIN cohort c ON c.id=o.decision_id GROUP BY o.decision_id), "
        "pairs AS (SELECT c.body_id,c.url_id,count(*) decisions, "
        "count(*) FILTER(WHERE p.decision_id IS NULL) unpaid,sum(p.n) n,sum(p.unknown) unknown, "
        "sum(p.cost) cost,sum(p.rejected) rejected,sum(p.passed) passed,sum(p.unresolved) unresolved "
        "FROM cohort c LEFT JOIN paid p ON p.decision_id=c.id GROUP BY c.body_id,c.url_id), "
        "funnel AS (SELECT k.stage,k.mode,k.action,sum(x.decisions)::bigint AS decisions, "
        "count(DISTINCT x.url_id) AS distinct_jobs, "
        "COALESCE(sum(x.n),0)::bigint recorded_outcomes, "
        "COALESCE(sum(x.unknown),0)::bigint unpriced_outcomes, "
        "COALESCE(sum(x.unpaid) FILTER(WHERE k.action='review'),0)::bigint without_recorded_outcome, "
        "COALESCE(sum(x.cost),0)::float known_cost_usd, "
        "CASE WHEN COALESCE(sum(x.unknown),0)=0 AND COALESCE(sum(x.unpaid) FILTER(WHERE k.action='review'),0)=0 "
        "THEN COALESCE(sum(x.cost),0)::float END actual_cost_usd, "
        "COALESCE(sum(x.rejected) FILTER(WHERE k.mode='shadow' AND k.stage<>'detailed'),0)::bigint agreed_reject, "
        "COALESCE(sum(x.passed) FILTER(WHERE k.mode='shadow' AND k.stage<>'detailed'),0)::bigint false_reject, "
        "COALESCE(sum(x.unresolved) FILTER(WHERE k.mode='shadow' AND k.stage<>'detailed'),0)::bigint unresolved "
        "FROM pairs x JOIN keys k ON k.id=x.body_id GROUP BY k.stage,k.mode,k.action), "
        "per_decision AS ("
        "SELECT c.id,k.prompt_hash,k.model,k.transport, "
        "sum(o.recorded_cost_usd)::float cost,count(o.id) n, "
        "count(*) FILTER(WHERE o.recorded_cost_usd IS NULL OR o.model IS DISTINCT FROM "
        "k.model) unknown "
        "FROM cohort c JOIN keys k ON k.id=c.body_id "
        "LEFT JOIN review_gate_outcomes o ON o.decision_id=c.id "
        "WHERE k.action='review' GROUP BY c.id,k.prompt_hash,k.model,k.transport), baseline AS ("
        "SELECT prompt_hash,model,transport,avg(cost)::float mean_cost,sum(n) n "
        "FROM per_decision GROUP BY prompt_hash,model,transport "
        "HAVING sum(unknown)=0), skipped AS ("
        "SELECT k.prompt_hash,k.model,k.transport,sum(c.n)::bigint n "
        "FROM (SELECT body_id,count(*) n FROM cohort GROUP BY body_id) c "
        "JOIN keys k ON k.id=c.body_id WHERE k.action='skip' "
        "GROUP BY k.prompt_hash,k.model,k.transport), estimate AS ("
        "SELECT CASE WHEN count(r.mean_cost)>0 THEN sum(s.n*r.mean_cost)::float END estimated_avoided_cost_usd, "
        "COALESCE(sum(s.n) FILTER(WHERE r.mean_cost IS NOT NULL),0)::bigint estimated_decisions, "
        "COALESCE(sum(s.n) FILTER(WHERE r.mean_cost IS NULL),0)::bigint unestimated_decisions, "
        "COALESCE(sum(r.n),0)::bigint reference_outcomes "
        "FROM skipped s LEFT JOIN baseline r USING(prompt_hash,model,transport)) "
        # JSON carries each float as its shortest round-trip text, so the
        # values are the ones a direct column would have returned.
        f"SELECT (SELECT min(d.created_at) FROM review_gate_decisions d WHERE {where}) AS first, "
        "(SELECT COALESCE(json_agg(f ORDER BY f.stage,f.mode,f.action),'[]') FROM funnel f) AS rows, "
        "(SELECT row_to_json(e) FROM estimate e) AS estimate",
        {**values, "start": start, "end": end},
    )
    assert report is not None
    return GateReport(
        generated_at=end,
        window_start=start,
        window_end=end,
        days=days,
        first_recorded_at=report["first"],
        rows=[GateFunnelRow.model_validate(row) for row in report["rows"]],
        filters=filters,
        avoided_cost=AvoidedCostEstimate.model_validate(report["estimate"]),
    )
