"""The notes visitors leave about a public list, and the administrator's answer.

A visitor's report about one posting is not here: it is a `reports` row and
reads in the posting report queue (`/admin/reports`), beside people's own.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db, pagination
from api import params as params_
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin.shared import require_admin

router = APIRouter()


class ListFeedbackNote(BaseModel):
    """`contact` is what the visitor typed, null when they left nothing; it is
    the only thing that identifies them."""

    id: int
    job_list_slug: str
    job_list_name: str
    message: str
    contact: str | None
    status: str
    resolution_note: str | None
    created_at: datetime.datetime
    resolved_at: datetime.datetime | None


class ListFeedbackInbox(BaseModel):
    rows: list[ListFeedbackNote]
    page: int
    page_size: int
    total: int
    has_more: bool
    filters: dict[str, list[str]]
    filterable: list[str]


@router.get("/job-list-feedback")
def list_job_list_feedback(
    status: str = "open",
    lists: str | None = Query(default=None, alias="list"),
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
) -> ListFeedbackInbox:
    paging = pagination.Page.from_params(page, page_size, maximum=200)
    statuses = [] if status.strip() == "all" else params_.csv(status)
    slugs = params_.csv(lists)
    clauses = []
    if statuses:
        clauses.append("f.status = ANY(%(status)s)")
    if slugs:
        clauses.append("b.slug = ANY(%(slugs)s)")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args = {"status": statuses, "slugs": slugs}
    total_row = db.query_one(
        "SELECT COUNT(*) AS c FROM job_list_feedback f "
        f"JOIN managed_boards b ON b.id = f.managed_board_id {where}",
        args,
    )
    rows = db.query_as(
        ListFeedbackNote,
        f"""
        SELECT f.id, b.slug AS job_list_slug, b.name AS job_list_name, f.message, f.contact,
               f.status, f.resolution_note, f.created_at, f.resolved_at
        FROM job_list_feedback f
        JOIN managed_boards b ON b.id = f.managed_board_id
        {where}
        ORDER BY f.id DESC LIMIT %(limit)s OFFSET %(offset)s
        """,
        args | {"limit": paging.size, "offset": paging.offset},
    )
    total = total_row["c"] if total_row else 0
    return ListFeedbackInbox(
        rows=rows,
        **paging.metadata(total),
        filters=params_.applied(status=statuses, list=slugs),
        filterable=["status", "list"],
    )


class ResolveListFeedback(BaseModel):
    action: str
    note: str = ""


class ListFeedbackResolved(BaseModel):
    id: int
    status: str


@router.post("/job-list-feedback/{feedback_id}/resolve")
def resolve_job_list_feedback(
    feedback_id: int, body: ResolveListFeedback, user: AuthedUser = Depends(require_admin)
) -> ListFeedbackResolved:
    """The same two answers a posting report takes, so the two queues read
    alike: resolved when acted on, dismissed when there was nothing to do.
    Only an open note takes one, so a second answer (a double click, another
    administrator on a stale inbox) cannot overwrite the first."""
    if body.action not in ("resolved", "dismissed"):
        raise refuse(400, "INVALID_ACTION", "action must be resolved or dismissed")
    row = db.query_one_as(
        ListFeedbackResolved,
        "UPDATE job_list_feedback SET status = %s, resolution_note = %s, resolved_at = now() "
        "WHERE id = %s AND status = 'open' RETURNING id, status",
        (body.action, body.note or None, feedback_id),
    )
    if row:
        return row
    current = db.query_one("SELECT status FROM job_list_feedback WHERE id = %s", (feedback_id,))
    if current is None:
        raise refuse(404, "NOT_FOUND", "unknown feedback")
    raise refuse(409, "ALREADY_ANSWERED", f"this note is already {current['status']}")
