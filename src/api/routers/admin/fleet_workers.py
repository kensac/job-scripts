"""Admin worker heartbeats, activity, and queue throughput routes."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db, health
from api.auth import AuthedUser
from api.routers.admin.shared import SUMMARY_MAX_HOURS, require_admin
from api.task_admission import TaskProgress

router = APIRouter()


class FleetWorker(BaseModel):
    """One worker, as its heartbeat and its current task leave it.

    `alive` is the heartbeat being younger than `health.WORKER_FRESH`, the
    window /queue's `fresh` uses, resolved in SQL so every reader agrees on
    the same clock. The task fields are null when the
    worker is idle: they come from a join on a task that is still running.
    A worker running several tasks at once (worker_task_slots) names them all
    in `current_task_ids` and its adaptive limit in `task_limit`; the task
    fields describe the oldest.
    """

    name: str
    started_at: datetime.datetime
    last_seen: datetime.datetime
    current_task_id: int | None
    current_task_ids: list[int]
    task_limit: int | None
    release: str | None
    alive: bool
    task_kind: str | None
    task_status: str | None
    task_progress: TaskProgress | None
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
        SELECT w.name, w.started_at, w.last_seen, w.current_task_id, w.current_task_ids,
               w.task_limit, w.release,
               w.last_seen > now() - %(fresh)s::interval AS alive,
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
        {"fresh": health.WORKER_FRESH},
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
    current_task_ids: list[int]
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
            SELECT name, started_at, last_seen, current_task_id, current_task_ids,
                   last_seen > now() - %(fresh)s::interval AS fresh
            FROM worker_status ORDER BY name
            """,
            {"fresh": health.WORKER_FRESH},
        ),
    )


# A batch's columns and its totals from the call ledger, named once, for the
