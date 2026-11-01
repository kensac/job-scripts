"""Admin provider batch list and per-verdict drill-down routes."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db, model_calls, scoping
from api import params as params_
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin.shared import require_admin
from core import pricing

listing_router = APIRouter()
detail_router = APIRouter()


_BATCH_SELECT_COLUMNS = (
    "b.id, b.provider_batch_id, b.task_id, b.purpose, b.model, b.requests, b.completed, "
    "b.failed_count, b.status, b.submitted_at, b.updated_at, b.completed_at, b.est_tokens, "
    "totals.input_tokens, totals.output_tokens, totals.cache_write_tokens, totals.est_cost_usd"
)


class BatchRecord(BaseModel):
    """One batch job at the provider.

    `requests` is what was submitted, `completed` and `failed_count` what came
    back. `est_tokens` is what submission estimated; `input_tokens` and
    `output_tokens` are what the provider billed, so the two disagreeing is
    the estimate being wrong rather than anything being lost.
    """

    id: int
    provider_batch_id: str
    task_id: int | None
    purpose: str
    model: str | None
    requests: int
    completed: int
    failed_count: int
    status: str
    submitted_at: datetime.datetime
    updated_at: datetime.datetime
    completed_at: datetime.datetime | None
    est_tokens: int
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int | None
    # float, not Decimal: a declared Decimal serialises as a JSON string and
    # this has always been a number on the wire.
    est_cost_usd: float | None


class ProviderBatch(BatchRecord):
    """A batch with the task it belongs to."""

    task_kind: str | None
    task_status: str | None


class ProviderBatches(BaseModel):
    rows: list[ProviderBatch]
    filters: dict[str, list[str]]
    filterable: list[str]


@listing_router.get("/batches")
def list_batches(
    hours: int = 72,
    users: str | None = Query(default=None, alias="user"),
    user: AuthedUser = Depends(require_admin),
) -> ProviderBatches:
    """Provider batch jobs: what's pending at OpenAI right now, and recent
    history. Pending first, then newest. A batch belongs to the user its
    task ran for; fleet batches belong to nobody."""
    hours = max(1, min(hours, 720))
    ids = scoping.user_ids(users)
    rows = db.query_as(
        ProviderBatch,
        f"""
        SELECT {_BATCH_SELECT_COLUMNS}, t.kind AS task_kind, t.status AS task_status
        FROM ai_batches b LEFT JOIN tasks t ON t.id = b.task_id
        {model_calls.BATCH_TOTALS}
        WHERE (b.status NOT IN ('completed', 'failed', 'expired', 'cancelled')
           OR b.submitted_at > now() - make_interval(hours => %(hours)s))
          {"AND " + scoping.task("t") if ids else ""}
        ORDER BY (b.status IN ('completed', 'failed', 'expired', 'cancelled')), b.id DESC
        LIMIT 200
        """,
        {"hours": hours, "user_ids": ids},
    )
    return ProviderBatches(
        rows=rows,
        filters=params_.applied(user=scoping.echo(ids)),
        filterable=["user"],
    )


class BatchedCheck(BaseModel):
    """One verdict this batch produced. `est_cost_usd` is not stored: it is
    priced here from the batch's model at the batched rate, so a batch is
    inspectable down to what each individual request cost."""

    id: int
    url: str | None
    check_type: str | None
    status: str | None
    reason: str | None
    company: str | None
    job_title: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None
    created_at: datetime.datetime
    source: str | None
    job_id: int | None
    est_cost_usd: float | None


class BatchJobs(BaseModel):
    batch: BatchRecord
    rows: list[BatchedCheck]
    total: int
    has_more: bool


@detail_router.get("/batches/{provider_batch_id}/jobs")
def batch_jobs(
    provider_batch_id: str,
    limit: int = 200,
    offset: int = 0,
    user: AuthedUser = Depends(require_admin),
) -> BatchJobs:
    """Every verdict this batch produced, with its own token cost, so a batch
    is inspectable down to the individual job. Paged: a batch is up to 500
    requests and the drill-down rendered every one of them."""
    batch = db.query_one_as(
        BatchRecord,
        f"SELECT {_BATCH_SELECT_COLUMNS} FROM ai_batches b {model_calls.BATCH_TOTALS} "
        "WHERE b.provider_batch_id = %s",
        (provider_batch_id,),
    )
    if not batch:
        raise refuse(404, "NOT_FOUND", "unknown batch")
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = db.query_one(
        "SELECT count(*) AS n FROM ai_queries WHERE batch_id = %s", (provider_batch_id,)
    )
    rows = db.query(
        """
        SELECT q.id, q.url, q.check_type, q.status, q.reason, q.company, q.job_title,
               q.prompt_tokens, q.completion_tokens, q.total_tokens, q.cached_tokens,
               q.cache_write_tokens,
               q.created_at, j.source, j.id AS job_id
        FROM ledger_rows q LEFT JOIN jobs j ON j.url = q.url
        WHERE q.batch_id = %s ORDER BY q.id LIMIT %s OFFSET %s
        """,
        (provider_batch_id, limit, offset),
    )
    priced_verdicts = []
    for verdict_row in rows:
        estimated_cost = pricing.estimate_cost_usd(
            batch.model,
            verdict_row["prompt_tokens"],
            verdict_row["completion_tokens"],
            cached_tokens=verdict_row.get("cached_tokens"),
            cache_write_tokens=verdict_row.get("cache_write_tokens"),
            batched=True,
        )
        priced_verdicts.append(
            BatchedCheck(
                **verdict_row,
                est_cost_usd=round(float(estimated_cost), 6)
                if estimated_cost is not None
                else None,
            )
        )
    total_verdicts = total["n"] if total else 0
    return BatchJobs(
        batch=batch,
        rows=priced_verdicts,
        total=total_verdicts,
        has_more=offset + len(priced_verdicts) < total_verdicts,
    )
