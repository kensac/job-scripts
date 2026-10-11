"""Admin task queue and cancellation routes."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api import db, queue, scoping, task_admission
from api import params as params_
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin.shared import require_admin

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
    selection_clauses: list[str] = []
    selected_user_ids = scoping.user_ids(users)
    statuses, kinds, sources, workers = (
        params_.csv(status),
        params_.csv(kind),
        params_.csv(source),
        params_.csv(worker),
    )
    params: dict[str, Any] = {"limit": limit + 1}
    if statuses:
        selection_clauses.append("status = ANY(%(status)s)")
        params["status"] = statuses
    if kinds:
        selection_clauses.append("kind = ANY(%(kind)s)")
        params["kind"] = kinds
    if sources:
        # Ingest tasks are most of the queue at 389 boards; this cuts it to
        # one board's history.
        selection_clauses.append("payload->>'source' = ANY(%(source)s)")
        params["source"] = sources
    if workers:
        # A health alert about a worker links here.
        selection_clauses.append("worker = ANY(%(worker)s)")
        params["worker"] = workers
    if selected_user_ids:
        selection_clauses.append(scoping.task())
        params["user_ids"] = selected_user_ids
    selection_where = f"WHERE {' AND '.join(selection_clauses)}" if selection_clauses else ""
    if before_id is not None:
        selection_clauses.append("id < %(before_id)s")
        params["before_id"] = before_id
    page_where = f"WHERE {' AND '.join(selection_clauses)}" if selection_clauses else ""
    rows = db.query_as(
        QueuedTask,
        f"""
        SELECT id, kind, payload, status, attempts, worker, progress, error,
               created_at, started_at, last_heartbeat, finished_at,
               status = ANY(%(cancellable)s) AS cancellable
        FROM tasks {page_where} ORDER BY id DESC LIMIT %(limit)s
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
            status=statuses,
            kind=kinds,
            source=sources,
            worker=workers,
            user=scoping.echo(selected_user_ids),
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
        raise refuse(400, "NO_SELECTION", "give ids, a kind, a source, or a status")
    if body.status is not None and body.status not in CANCELLABLE:
        raise refuse(400, "NOT_CANCELLABLE", f"status must be one of {', '.join(CANCELLABLE)}")
    cancelled = queue.cancel(
        CANCELLABLE, ids=body.ids, kind=body.kind, status=body.status, source=body.source
    )
    return TasksCancelled(
        cancelled=cancelled,
        skipped=[i for i in (body.ids or []) if i not in cancelled],
    )
