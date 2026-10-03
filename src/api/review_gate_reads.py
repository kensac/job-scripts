"""Read models over durable review decisions, never inferred from current filters."""

from __future__ import annotations

import datetime

from pydantic import BaseModel, JsonValue

from api import db, pagination, review_policy_storage


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
    f"{review_policy_storage.POLICY_COLUMNS},d.evidence,d.created_at"
)


def read_decisions(
    where: str,
    parameters: dict,
    page: pagination.Page,
    filters: dict[str, list[str]],
    *,
    personal: bool = False,
) -> ReviewDecisions:
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return _read_decisions(where, parameters, page, filters, personal=personal)


def _read_decisions(
    where: str,
    parameters: dict,
    page: pagination.Page,
    filters: dict[str, list[str]],
    *,
    personal: bool,
) -> ReviewDecisions:
    count = db.query_one(
        f"SELECT count(*) AS n FROM review_gate_decisions d WHERE {where}", parameters
    )
    raw_rows = db.query(
        f"SELECT {DECISION_COLUMNS} FROM review_gate_decisions d "
        f"{review_policy_storage.POLICY_JOIN} WHERE {where} "
        "ORDER BY d.id DESC LIMIT %(limit)s OFFSET %(offset)s",
        {**parameters, "limit": page.size, "offset": page.offset},
    )
    rows = [ReviewDecision.model_validate(review_policy_storage.resolve(row)) for row in raw_rows]
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
            "filter_id",
            "window_start",
            "window_end",
        ],
    )


def comparisons(task_id: int) -> dict[str, dict[str, int]]:
    """Shadow proposals against the paid verdict, derived from durable rows.

    Each paid batch result records its outcome against the decision that
    carried the proposal, inside the receipt transaction, so replay counts
    once. Counting into tasks.payload instead rewrote a managed batch's whole
    TOASTed job list per receipt: measured 2026-10-03, about 104 GB rewritten
    across 259 batches for counters nothing read.
    """
    rows = db.query(
        "SELECT 'routing' AS kind, CASE WHEN o.rejected IS NULL THEN 'unresolved_reference' "
        "WHEN d.evidence->'routing'->>'outcome'='abstain' THEN 'abstained' "
        "WHEN (d.evidence->'routing'->>'outcome'='reject')=o.rejected THEN 'agreed' "
        "WHEN d.evidence->'routing'->>'outcome'='reject' THEN 'false_reject' "
        "ELSE 'false_accept' END AS key, count(*) AS n "
        "FROM review_gate_decisions d JOIN review_gate_outcomes o ON o.decision_id=d.id "
        "WHERE d.task_id=%(task)s AND jsonb_typeof(d.evidence->'routing')='object' GROUP BY 2 "
        "UNION ALL SELECT 'review_gate', CASE WHEN o.rejected IS NULL THEN 'unresolved' "
        "WHEN o.rejected THEN 'agreed_reject' ELSE 'false_reject' END, count(*) "
        "FROM review_gate_decisions d JOIN review_gate_outcomes o ON o.decision_id=d.id "
        "WHERE d.task_id=%(task)s AND d.stage<>'detailed' GROUP BY 2",
        {"task": task_id},
    )
    result: dict[str, dict[str, int]] = {"routing": {}, "review_gate": {}}
    for row in rows:
        result[row["kind"]][row["key"]] = row["n"]
    return result
