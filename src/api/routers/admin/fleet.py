"""What the fleet is doing: the task queue, the workers running it, and
the provider batches a task is parked on."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import db, events, health, scoping, task_admission
from api import params as params_
from api.auth import AuthedUser
from api.routers.admin.shared import SUMMARY_MAX_HOURS, require_admin
from core import pricing

router = APIRouter()


class QueuedTask(BaseModel):
    """One row of the queue. `payload` is the task's subject and `progress` is
    what the handler has said about itself so far, both shaped by the kind, so
    neither is declared further.

    `cancellable` is not a column: it is whether the row's status is one a
    cancel would reach, resolved on read so the page does not carry a second
    copy of that list.
    """

    id: int
    kind: str
    payload: dict[str, Any]
    status: str
    attempts: int
    worker: str | None
    progress: dict[str, Any] | None
    error: str | None
    created_at: datetime.datetime
    started_at: datetime.datetime | None
    last_heartbeat: datetime.datetime | None
    finished_at: datetime.datetime | None
    cancellable: bool


class KindStatusCount(BaseModel):
    kind: str
    status: str
    count: int


class TaskQueue(BaseModel):
    """`summary` counts the whole selection, not the page, so a filtered view
    says how much it matched rather than how much it drew.

    `statuses` is the lifecycle in order, which is what lets the page render
    them in that order rather than alphabetically.
    """

    rows: list[QueuedTask]
    has_more: bool
    summary: list[KindStatusCount]
    filters: dict[str, list[str]]
    filterable: list[str]
    statuses: list[str]


@router.get("/tasks")
def list_tasks(
    status: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    worker: str | None = None,
    users: str | None = Query(default=None, alias="user"),
    limit: int = 100,
    before_id: int | None = None,
    user: AuthedUser = Depends(require_admin),
) -> TaskQueue:
    # id-cursor pagination: stable under new tasks arriving while the admin
    # loads more (offset would shift and duplicate rows).
    limit = max(1, min(limit, 500))
    clauses: list[str] = []
    ids = scoping.user_ids(users)
    statuses, kinds, sources, workers = (
        params_.csv(status),
        params_.csv(kind),
        params_.csv(source),
        params_.csv(worker),
    )
    params: dict[str, Any] = {"limit": limit + 1}
    if statuses:
        clauses.append("status = ANY(%(status)s)")
        params["status"] = statuses
    if kinds:
        clauses.append("kind = ANY(%(kind)s)")
        params["kind"] = kinds
    if sources:
        # Ingest tasks are most of the queue at 389 boards; this cuts it to
        # one board's history.
        clauses.append("payload->>'source' = ANY(%(source)s)")
        params["source"] = sources
    if workers:
        # A health alert about a worker links here.
        clauses.append("worker = ANY(%(worker)s)")
        params["worker"] = workers
    if ids:
        clauses.append(scoping.task())
        params["user_ids"] = ids
    selection_where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    if before_id is not None:
        clauses.append("id < %(before_id)s")
        params["before_id"] = before_id
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query_as(
        QueuedTask,
        f"""
        SELECT id, kind, payload, status, attempts, worker, progress, error,
               created_at, started_at, last_heartbeat, finished_at,
               status = ANY(%(cancellable)s) AS cancellable
        FROM tasks {where} ORDER BY id DESC LIMIT %(limit)s
        """,
        {**params, "cancellable": list(CANCELLABLE)},
    )
    summary = db.query_as(
        KindStatusCount,
        f"SELECT kind, status, COUNT(*) AS count FROM tasks "
        f"{selection_where} "
        "GROUP BY kind, status ORDER BY kind, status",
        params,
    )
    return TaskQueue(
        rows=rows[:limit],
        has_more=len(rows) > limit,
        summary=summary,
        filters=params_.applied(
            status=statuses, kind=kinds, source=sources, worker=workers, user=scoping.echo(ids)
        ),
        filterable=["status", "kind", "source", "worker", "user"],
        statuses=list(TASK_STATUSES),
    )


# Preserve lifecycle order in queue metadata and cancellation validation.
TASK_STATUSES = task_admission.TASK_STATUSES


CANCELLABLE = tuple(status for status in TASK_STATUSES if status in task_admission.ACTIVE_STATUSES)


class CancelTasksBody(BaseModel):
    # Either specific ids, or a selection by kind / source / status, or both
    # (intersected). A selection cancels everything that matches, however
    # many: "cancel every pending ingest" is one write, not one page of it.
    ids: list[int] | None = Field(default=None, max_length=500)
    kind: str | None = None
    source: str | None = None
    status: str | None = None


class TasksCancelled(BaseModel):
    """`skipped` names only ids the caller asked for by id and that were not
    cancellable. A selection by kind or status cancels whatever matches, so
    there is nothing for it to skip."""

    cancelled: list[int]
    skipped: list[int]


@router.post("/tasks/cancel")
def cancel_tasks(
    body: CancelTasksBody, user: AuthedUser = Depends(require_admin)
) -> TasksCancelled:
    """Cancel queued or in-flight tasks. Running workers notice via their
    mid-task cancellation checks; pending chunks of a cancelled parent are
    swept by the worker's reconciler. A task parked on provider batches
    (awaiting_batch) is cancellable too - it holds no worker, so nothing
    notices otherwise and it would sit until its batches landed."""
    if body.ids is None and body.kind is None and body.source is None and body.status is None:
        raise HTTPException(
            400,
            detail={"code": "NO_SELECTION", "message": "give ids, a kind, a source, or a status"},
        )
    if body.status is not None and body.status not in CANCELLABLE:
        raise HTTPException(
            400,
            detail={
                "code": "NOT_CANCELLABLE",
                "message": f"status must be one of {', '.join(CANCELLABLE)}",
            },
        )
    clauses = ["status = ANY(%(cancellable)s)"]
    params: dict[str, Any] = {"cancellable": list(CANCELLABLE)}
    for key, value in (("id", body.ids), ("kind", body.kind), ("status", body.status)):
        if value is not None:
            clauses.append(f"{key} = ANY(%({key})s)" if key == "id" else f"{key} = %({key})s")
            params[key] = value
    if body.source is not None:
        clauses.append("payload->>'source' = %(source)s")
        params["source"] = body.source
    rows = db.query(
        f"""
        UPDATE tasks SET status = 'cancelled', error = 'cancelled by admin',
                         finished_at = now()
        WHERE {" AND ".join(clauses)}
        RETURNING id
        """,
        params,
    )
    cancelled = [r["id"] for r in rows]
    for task_id in cancelled:
        events.publish_task(task_id)
    return TasksCancelled(
        cancelled=cancelled,
        skipped=[i for i in (body.ids or []) if i not in cancelled],
    )


class ProviderBatch(BaseModel):
    """One batch job at the provider, with the task it belongs to.

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
    est_tokens: int
    input_tokens: int
    output_tokens: int
    # float, not Decimal: a declared Decimal serialises as a JSON string and
    # this has always been a number on the wire.
    est_cost_usd: float | None
    submitted_at: datetime.datetime
    updated_at: datetime.datetime
    completed_at: datetime.datetime | None
    task_kind: str | None
    task_status: str | None


class ProviderBatches(BaseModel):
    rows: list[ProviderBatch]
    filters: dict[str, list[str]]
    filterable: list[str]


@router.get("/batches")
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
        SELECT b.id, b.provider_batch_id, b.task_id, b.purpose, b.model,
               b.requests, b.completed, b.failed_count, b.status,
               b.est_tokens, b.input_tokens, b.output_tokens, b.est_cost_usd,
               b.submitted_at, b.updated_at, b.completed_at,
               t.kind AS task_kind, t.status AS task_status
        FROM ai_batches b LEFT JOIN tasks t ON t.id = b.task_id
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


class FleetWorker(BaseModel):
    """One worker, as its heartbeat and its current task leave it.

    `alive` is the heartbeat being under 90 seconds old, resolved in SQL so
    every reader agrees on the same clock. The task fields are null when the
    worker is idle: they come from a join on a task that is still running.
    """

    name: str
    started_at: datetime.datetime
    last_seen: datetime.datetime
    current_task_id: int | None
    release: str | None
    alive: bool
    task_kind: str | None
    task_status: str | None
    task_progress: dict[str, Any] | None
    task_started_at: datetime.datetime | None
    done_24h: int | None
    failed_24h: int | None


class FleetWorkers(BaseModel):
    rows: list[FleetWorker]


@router.get("/workers")
def list_workers(user: AuthedUser = Depends(require_admin)) -> FleetWorkers:
    """Live fleet view: every worker's heartbeat, what it's running right now,
    and its last-24h throughput."""
    rows = db.query_as(
        FleetWorker,
        """
        SELECT w.name, w.started_at, w.last_seen, w.current_task_id, w.release,
               now() - w.last_seen < interval '90 seconds' AS alive,
               t.kind AS task_kind, t.status AS task_status, t.progress AS task_progress,
               t.started_at AS task_started_at,
               stats.done_24h, stats.failed_24h
        FROM worker_status w
        LEFT JOIN tasks t ON t.id = w.current_task_id AND t.status = 'running'
        LEFT JOIN LATERAL (
            SELECT COUNT(*) FILTER (WHERE s.status = 'done') AS done_24h,
                   COUNT(*) FILTER (WHERE s.status = 'failed') AS failed_24h
            FROM tasks s WHERE s.worker = w.name
              AND s.finished_at > now() - interval '24 hours'
        ) stats ON TRUE
        ORDER BY w.name
        """,
    )
    return FleetWorkers(rows=rows)


class PendingKind(BaseModel):
    """What is waiting, and how long the oldest of it has waited. The age is
    what turns a pending count into "something is stuck"."""

    kind: str
    pending: int
    oldest_minutes: int | None


class RunningTask(BaseModel):
    """A task a worker holds right now. `source` is lifted out of the payload
    because an ingest is most of the queue and the board is what identifies
    it; every other kind has none."""

    id: int
    kind: str
    worker: str | None
    status: str
    started_at: datetime.datetime | None
    progress: dict[str, Any] | None
    source: str | None


class ThroughputBucket(BaseModel):
    """One hour of one worker on one kind. This is what makes a pending count
    an ETA, so it is per worker and per kind rather than a fleet average."""

    hour: datetime.datetime
    worker: str | None
    kind: str
    done: int
    failed: int
    avg_seconds: int | None


class WorkerHeartbeat(BaseModel):
    """The fleet, thinner than /workers: no throughput and no running task,
    because this answers "is anybody there" beside the queue numbers."""

    name: str
    started_at: datetime.datetime
    last_seen: datetime.datetime
    current_task_id: int | None
    fresh: bool


class QueueSummary(BaseModel):
    hours: int
    pending: list[PendingKind]
    in_flight: list[RunningTask]
    throughput: list[ThroughputBucket]
    workers: list[WorkerHeartbeat]


@router.get("/queue")
def queue_summary(hours: int = 6, user: AuthedUser = Depends(require_admin)) -> QueueSummary:
    """The queue as numbers a person can act on: what is waiting, for how
    long, per kind; who is doing what; and the fleet's throughput per hour,
    which is what turns a pending count into an ETA."""
    hours = max(1, min(hours, SUMMARY_MAX_HOURS))
    return QueueSummary(
        hours=hours,
        pending=db.query_as(
            PendingKind,
            """
            SELECT kind, COUNT(*) AS pending,
                   round(EXTRACT(EPOCH FROM now() - MIN(created_at)) / 60) AS oldest_minutes
            FROM tasks WHERE status = 'pending' GROUP BY kind ORDER BY pending DESC
            """,
        ),
        in_flight=db.query_as(
            RunningTask,
            """
            SELECT id, kind, worker, status, started_at, progress,
                   payload->>'source' AS source
            FROM tasks WHERE status IN ('running', 'waiting', 'awaiting_batch')
            ORDER BY started_at
            """,
        ),
        throughput=db.query_as(
            ThroughputBucket,
            """
            SELECT date_trunc('hour', finished_at) AS hour, worker, kind,
                   COUNT(*) FILTER (WHERE status = 'done') AS done,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                   round(avg(EXTRACT(EPOCH FROM finished_at - started_at))) AS avg_seconds
            FROM tasks
            WHERE finished_at > now() - make_interval(hours => %(hours)s)
              AND started_at IS NOT NULL
            GROUP BY 1, 2, 3 ORDER BY 1 DESC, 2, 3
            """,
            {"hours": hours},
        ),
        workers=db.query_as(
            WorkerHeartbeat,
            """
            SELECT name, started_at, last_seen, current_task_id,
                   last_seen > now() - %(fresh)s::interval AS fresh
            FROM worker_status ORDER BY name
            """,
            {"fresh": health.WORKER_FRESH},
        ),
    )


# Every column of ai_batches, named once, for the drill-down that read
# SELECT * to get at one of them.
_BATCH_COLS = (
    "id, provider_batch_id, task_id, purpose, model, requests, completed, failed_count, "
    "status, submitted_at, updated_at, completed_at, est_tokens, input_tokens, "
    "output_tokens, est_cost_usd, prompt_id"
)


class BatchRecord(BaseModel):
    """A batch as stored. `prompt_id` is the prompt version it was submitted
    under, which is what makes a batch's answers attributable to a prompt
    rather than to a model alone."""

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
    est_cost_usd: float | None
    prompt_id: int | None


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
    created_at: datetime.datetime
    source: str | None
    job_id: int | None
    est_cost_usd: float | None


class BatchJobs(BaseModel):
    batch: BatchRecord
    rows: list[BatchedCheck]
    total: int
    has_more: bool


@router.get("/batches/{provider_batch_id}/jobs")
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
        f"SELECT {_BATCH_COLS} FROM ai_batches WHERE provider_batch_id = %s",
        (provider_batch_id,),
    )
    if not batch:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown batch"})
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = db.query_one(
        "SELECT count(*) AS n FROM ai_queries WHERE batch_id = %s", (provider_batch_id,)
    )
    rows = db.query(
        """
        SELECT q.id, q.url, q.check_type, q.status, q.reason, q.company, q.job_title,
               q.prompt_tokens, q.completion_tokens, q.total_tokens, q.cached_tokens,
               q.created_at, j.source, j.id AS job_id
        FROM ai_queries q LEFT JOIN jobs j ON j.url = q.url
        WHERE q.batch_id = %s ORDER BY q.id LIMIT %s OFFSET %s
        """,
        (provider_batch_id, limit, offset),
    )
    priced = []
    for r in rows:
        cost = pricing.estimate_cost_usd(
            batch.model,
            r["prompt_tokens"],
            r["completion_tokens"],
            cached_tokens=r.get("cached_tokens"),
            batched=True,
        )
        priced.append(
            BatchedCheck(**r, est_cost_usd=round(float(cost), 6) if cost is not None else None)
        )
    n = total["n"] if total else 0
    return BatchJobs(batch=batch, rows=priced, total=n, has_more=offset + len(priced) < n)
