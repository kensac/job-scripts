"""Reports a person files about a catalog posting."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.board.access import require_visible_job
from api.problem import refuse
from api.reports import REPORT_KINDS, request_recheck

router = APIRouter()


class JobReport(BaseModel):
    kind: str
    message: str = ""
    corrections: dict | None = None


class ReportFiled(BaseModel):
    id: int
    status: str
    created_at: datetime.datetime


@router.post("/user/jobs/{job_id}/report")
def report_job(
    job_id: int, body: JobReport, user: AuthedUser = Depends(require_user)
) -> ReportFiled:
    if body.kind not in REPORT_KINDS:
        raise refuse(400, "INVALID_KIND", f"kind must be one of {REPORT_KINDS}")
    job = require_visible_job(user, job_id, "j.url, j.company, j.title")
    row = db.query_one_as(
        ReportFiled,
        """
        INSERT INTO reports (user_id, job_id, kind, message, corrections)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, status, created_at
        """,
        (
            user.id,
            job_id,
            body.kind,
            body.message,
            db.jsonb(body.corrections) if body.corrections is not None else None,
        ),
    )
    assert row is not None  # an insert with RETURNING always yields its row
    if body.kind == "closed":
        request_recheck(job)
    return row
