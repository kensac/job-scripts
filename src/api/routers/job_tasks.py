"""Status for work a person started."""

from __future__ import annotations

import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.task_admission import TaskProgress

router = APIRouter()


class TaskState(BaseModel):
    """A task this person started. Ownership lives in the payload, so the
    kinds that stamp no user_id are fleet work with nobody to show them to."""

    id: int
    kind: str
    status: Literal[
        "pending", "waiting", "running", "awaiting_batch", "done", "failed", "cancelled"
    ]
    progress: TaskProgress | None
    error: str | None
    created_at: datetime.datetime
    started_at: datetime.datetime | None
    finished_at: datetime.datetime | None


@router.get("/tasks/{task_id}")
def get_task(task_id: int, user: AuthedUser = Depends(require_user)) -> TaskState:
    # Task ids are sequential and `error` is str(exc) written verbatim by the
    # worker, so an ungated lookup hands any signed-in user every other user's
    # failures. Ownership lives in the payload: every user-initiated kind
    # stamps user_id there, and the kinds that do not (ingest_source,
    # verify_new, data_health...) are fleet work with no user to show it to.
    row = db.query_one_as(
        TaskState,
        "SELECT id, kind, status, progress, error, created_at, started_at, finished_at "
        "FROM tasks WHERE id = %s AND (payload->>'user_id')::bigint = %s",
        (task_id, user.id),
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown task"})
    return row
