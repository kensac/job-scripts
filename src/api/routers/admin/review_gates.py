"""Decision coverage and funnels, separate from the provider invoice ledger."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
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
    action: str | None = None,
    user: str | None = None,
    managed_board_id: int | None = None,
) -> tuple[str, dict, dict[str, list[str]]]:
    clauses, values, filters = [], {}, {}
    for key, value in {"url": url, "prompt_hash": prompt_hash, "stage": stage, "action": action}.items():
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
    action: str | None = None,
    user: str | None = None,
    managed_board_id: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    admin: AuthedUser = Depends(require_admin),
) -> ReviewDecisions:
    where, values, filters = selection(
        url=url, prompt_hash=prompt_hash, stage=stage, action=action,
        user=user, managed_board_id=managed_board_id,
    )
    return read_decisions(
        where, values, pagination.Page.from_params(page, page_size, maximum=100), filters
    )


class GateFunnelRow(BaseModel):
    stage: str
    mode: str
    action: str
    decisions: int
    distinct_jobs: int


class GateReport(BaseModel):
    generated_at: datetime.datetime
    window_start: datetime.datetime
    window_end: datetime.datetime
    days: int
    population: str = "Review decisions made during the window, not unique postings or provider calls."
    coverage: str = "Durable decisions only; earlier task-only history is unavailable."
    first_recorded_at: datetime.datetime | None
    rows: list[GateFunnelRow]
    filters: dict[str, list[str]]
    filterable: list[str] = ["prompt_hash", "user", "managed_board_id"]
    estimated_avoided_cost_usd: float | None = None
    estimate_basis: str = "Unavailable until a comparable recorded review-cost cohort is available."


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
    rows = db.query_as(
        GateFunnelRow,
        "SELECT d.stage,d.mode,d.action,count(*) AS decisions,count(DISTINCT d.url) AS distinct_jobs "
        f"FROM review_gate_decisions d WHERE {where} "
        "AND d.created_at >= %(start)s AND d.created_at < %(end)s "
        "GROUP BY d.stage,d.mode,d.action ORDER BY d.stage,d.mode,d.action",
        {**values, "start": start, "end": end},
    )
    return GateReport(
        generated_at=end, window_start=start, window_end=end, days=days,
        first_recorded_at=first["first"] if first else None, rows=rows, filters=filters,
    )
