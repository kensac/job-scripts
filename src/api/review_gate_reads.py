"""Read models over durable review decisions, never inferred from current filters."""

from __future__ import annotations

import datetime

from pydantic import BaseModel, JsonValue

from api import db, pagination


class ReviewOutcome(BaseModel):
    query_id: int
    batch_id: str | None
    model: str | None
    rejected: bool | None
    outcome: str
    recorded_cost_usd: float | None
    created_at: datetime.datetime


class ReviewDecision(BaseModel):
    id: int
    task_id: int
    url: str
    job_id: int | None
    user_id: int | None
    filter_id: int | None
    managed_board_id: int | None
    revision: int | None
    prompt_hash: str
    stage: str
    mode: str
    action: str
    reason: str | None
    profile_id: int | None
    title: str
    content_hash: str | None
    policy: dict[str, JsonValue]
    evidence: dict[str, JsonValue]
    created_at: datetime.datetime
    outcomes: list[ReviewOutcome] = []


class ReviewDecisions(BaseModel):
    rows: list[ReviewDecision]
    page: int
    page_size: int
    total: int
    has_more: bool
    filters: dict[str, list[str]]
    filterable: list[str]
    coverage: str = "Durable decisions only. Earlier task-only history is not reconstructed."


DECISION_COLUMNS = (
    "d.id,d.task_id,d.url,d.job_id,d.user_id,d.filter_id,d.managed_board_id,d.revision,"
    "d.prompt_hash,d.stage,d.mode,d.action,d.reason,d.profile_id,d.title,d.content_hash,"
    "d.policy,d.evidence,d.created_at"
)


def read_decisions(
    where: str,
    parameters: dict,
    page: pagination.Page,
    filters: dict[str, list[str]],
    *,
    personal: bool = False,
) -> ReviewDecisions:
    count = db.query_one(
        f"SELECT count(*) AS n FROM review_gate_decisions d WHERE {where}", parameters
    )
    rows = db.query_as(
        ReviewDecision,
        f"SELECT {DECISION_COLUMNS} FROM review_gate_decisions d WHERE {where} "
        "ORDER BY d.id DESC LIMIT %(limit)s OFFSET %(offset)s",
        {**parameters, "limit": page.size, "offset": page.offset},
    )
    if rows:
        outcomes = db.query(
            "SELECT decision_id,query_id,batch_id,model,rejected,outcome,recorded_cost_usd,created_at "
            "FROM review_gate_outcomes WHERE decision_id=ANY(%s) ORDER BY id",
            ([row.id for row in rows],),
        )
        by_id: dict[int, list[ReviewOutcome]] = {}
        for outcome in outcomes:
            decision_id = outcome.pop("decision_id")
            by_id.setdefault(decision_id, []).append(ReviewOutcome.model_validate(outcome))
        rows = [row.model_copy(update={"outcomes": by_id.get(row.id, [])}) for row in rows]
    if personal:
        # The whole policy contains other opt-in hashes. The owner needs their
        # decision, not an administrator's configuration of unrelated filters.
        rows = [row.model_copy(update={"policy": {}, "evidence": {}}) for row in rows]
    return ReviewDecisions(
        rows=rows,
        **page.metadata(count["n"] if count else 0),
        filters=filters,
        filterable=[]
        if personal
        else [
            "url",
            "prompt_hash",
            "stage",
            "mode",
            "action",
            "user",
            "managed_board_id",
            "window_start",
            "window_end",
        ],
    )
