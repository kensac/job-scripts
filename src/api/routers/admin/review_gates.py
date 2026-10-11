"""The title-screen funnel, derived from postings, verdicts and `title_screens`.

Nothing records a skip: a screen leaves a posting out of a candidate SELECT
(core/screening.py), so what it skipped is the screen run over the postings
again. Per-run decision rows were stored until 2026-10, 93.6% of 9.76M of them
repeating one already stored, and the derived skip set matched every stored one.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, JsonValue

from api import db, model_calls, scoping
from api.auth import AuthedUser
from api.routers.admin.shared import require_admin
from core import screening

router = APIRouter()

NOT_STORED = (
    "Per-run review decisions are no longer stored. The posting's path shows each "
    "board's and filter's title screen, evaluated now."
)


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
    """Always empty. The shape stays until the decision history panels that
    read it are removed from the job drawers."""

    rows: list[ReviewDecision] = []
    page: int = 1
    page_size: int = 25
    total: int = 0
    has_more: bool = False
    filters: dict[str, list[str]] = {}
    filterable: list[str] = []
    coverage: str = NOT_STORED


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
    agreed_reject: int = 0
    false_reject: int = 0
    unresolved: int = 0


class AvoidedCostEstimate(BaseModel):
    estimated_avoided_cost_usd: float | None
    estimated_decisions: int
    unestimated_decisions: int
    reference_outcomes: int
    basis: str = (
        "Screened postings times the mean recorded cost of a review under the same prompt "
        "in the same window. A workload-dependent estimate, not billed savings."
    )


class GateReport(BaseModel):
    generated_at: datetime.datetime
    window_start: datetime.datetime
    window_end: datetime.datetime
    days: int
    population: str = (
        "Skip: postings first seen in the window from a screened board's or filter's "
        "sources whose title its screen skips, one per prompt. Review: model calls under "
        "a screened prompt in the window."
    )
    coverage: str = (
        "Derived when read from today's title_screens; a screen changed during the window "
        "is applied as it stands now."
    )
    first_recorded_at: datetime.datetime | None = None
    rows: list[GateFunnelRow]
    filters: dict[str, list[str]]
    filterable: list[str] = ["prompt_hash", "user", "managed_board_id", "filter_id"]
    avoided_cost: AvoidedCostEstimate
    actual_cost_basis: str = (
        "Stored costs of the window's review calls under screened prompts, retries included. "
        "Not total platform spend or the provider invoice."
    )


# Every paid review in the report's range, failed ones included (a failure
# was paid for), each with its call's cost (model_calls.answers_with_usage).
_REVIEWS = model_calls.answers_with_usage(
    "q.check_type = 'custom' AND q.status IN ('passed', 'rejected', 'failed') "
    "AND q.prompt_hash IN (SELECT prompt_hash FROM targets) "
    "AND q.created_at >= %(start)s AND q.created_at < %(end)s"
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
    end = datetime.datetime.now(datetime.UTC)
    start = end - datetime.timedelta(days=days)
    users = scoping.user_ids(user)
    filters = {
        key: values
        for key, values in {
            "prompt_hash": [prompt_hash] if prompt_hash else [],
            "user": scoping.echo(users),
            "managed_board_id": [str(managed_board_id)] if managed_board_id else [],
            "filter_id": [str(filter_id)] if filter_id else [],
        }.items()
        if values
    }
    # A board is in scope unless a filter is named; a filter unless a board is.
    row = db.query_one(
        f"""
        WITH screens AS (
          SELECT key AS prompt_hash, value #>> '{{}}' AS recipe
          FROM jsonb_each(%(screens)s::jsonb)
          WHERE %(hash)s::text IS NULL OR key = %(hash)s
        ), targets AS (
          SELECT s.prompt_hash, s.recipe, src.source
          FROM screens s JOIN managed_boards b ON b.prompt_hash = s.prompt_hash
          JOIN managed_board_sources src ON src.managed_board_id = b.id
          WHERE %(filter)s::bigint IS NULL
            AND (%(board)s::bigint IS NULL OR b.id = %(board)s)
            AND (cardinality(%(users)s::bigint[]) = 0 OR b.sponsor_user_id = ANY(%(users)s))
          UNION
          SELECT s.prompt_hash, s.recipe, us.source
          FROM screens s JOIN user_filters f ON f.prompt_hash = s.prompt_hash AND f.enabled
          JOIN user_source_set us ON us.user_id = f.user_id
          WHERE %(board)s::bigint IS NULL
            AND (%(filter)s::bigint IS NULL OR f.id = %(filter)s)
            AND (cardinality(%(users)s::bigint[]) = 0 OR f.user_id = ANY(%(users)s))
        ), skips AS (
          SELECT t.prompt_hash, j.id
          FROM jobs j JOIN targets t ON t.source = j.source
          WHERE j.created_at >= %(start)s AND j.created_at < %(end)s
            AND {screening.skips_sql("t.recipe")}
          GROUP BY t.prompt_hash, j.id
        ), reviews AS (
          -- Every paid review, failed ones included: a failure was paid for.
          SELECT r.prompt_hash, r.url, r.cost_usd FROM {_REVIEWS} r
        ), mean AS (
          SELECT prompt_hash, avg(cost_usd)::float AS cost, count(*) AS n FROM reviews
          GROUP BY prompt_hash HAVING count(*) FILTER (WHERE cost_usd IS NULL) = 0
        ), skipped AS (
          SELECT prompt_hash, count(*) AS n FROM skips GROUP BY prompt_hash
        )
        SELECT
          (SELECT count(*) FROM skips) AS skip_decisions,
          (SELECT count(DISTINCT id) FROM skips) AS skip_jobs,
          (SELECT count(*) FROM reviews) AS reviews,
          (SELECT count(DISTINCT url) FROM reviews) AS review_jobs,
          (SELECT count(*) FILTER (WHERE cost_usd IS NULL) FROM reviews) AS unpriced,
          (SELECT COALESCE(sum(cost_usd), 0)::float FROM reviews) AS cost,
          (SELECT sum(s.n * m.cost)::float FROM skipped s JOIN mean m USING (prompt_hash))
            AS avoided,
          (SELECT COALESCE(sum(s.n) FILTER (WHERE m.cost IS NOT NULL), 0)
             FROM skipped s LEFT JOIN mean m USING (prompt_hash)) AS estimated,
          (SELECT COALESCE(sum(s.n) FILTER (WHERE m.cost IS NULL), 0)
             FROM skipped s LEFT JOIN mean m USING (prompt_hash)) AS unestimated,
          (SELECT COALESCE(sum(n), 0) FROM mean) AS reference
        """,
        {
            "screens": db.jsonb(db.get_config("title_screens")),
            "hash": prompt_hash,
            "board": managed_board_id,
            "filter": filter_id,
            "users": users,
            "start": start,
            "end": end,
            **screening.PARAMS,
        },
    )
    assert row is not None
    rows = []
    if row["skip_decisions"]:
        rows.append(
            GateFunnelRow(
                stage="title",
                mode="enforce",
                action="skip",
                decisions=row["skip_decisions"],
                distinct_jobs=row["skip_jobs"],
                recorded_outcomes=0,
                unpriced_outcomes=0,
                without_recorded_outcome=0,
                known_cost_usd=0.0,
                actual_cost_usd=0.0,
            )
        )
    if row["reviews"]:
        rows.append(
            GateFunnelRow(
                stage="detailed",
                mode="enforce",
                action="review",
                decisions=row["reviews"],
                distinct_jobs=row["review_jobs"],
                recorded_outcomes=row["reviews"],
                unpriced_outcomes=row["unpriced"],
                without_recorded_outcome=0,
                known_cost_usd=row["cost"],
                actual_cost_usd=None if row["unpriced"] else row["cost"],
            )
        )
    return GateReport(
        generated_at=end,
        window_start=start,
        window_end=end,
        days=days,
        rows=rows,
        filters=filters,
        avoided_cost=AvoidedCostEstimate(
            estimated_avoided_cost_usd=row["avoided"],
            estimated_decisions=row["estimated"],
            unestimated_decisions=row["unestimated"],
            reference_outcomes=row["reference"],
        ),
    )
