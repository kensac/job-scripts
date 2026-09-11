from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import db, events
from api.auth import AuthedUser, require_user
from api.board.person_state import touchable_job_ids, write_board_row
from api.models import UserJobPatch, UserJobsBulkIds, UserJobsBulkPatch
from api.problem import refuse

patch_router = APIRouter()
delete_router = APIRouter()


class BulkDeleted(BaseModel):
    ok: bool
    deleted: int


class Autofilled(BaseModel):
    """What the write filled in that the caller did not send.

    A status change can date the row by itself, so the caller is told rather
    than having to re-read the row to find out.

    An absent key is the answer, not a null: the route sets
    response_model_exclude_none so `{}` still means "nothing was filled",
    which is what the board already reads. Declaring the shape must not move
    the wire.
    """

    status: str | None = None
    date_applied: datetime.date | None = None


class PatchResult(BaseModel):
    ok: bool
    autofilled: Autofilled


class BulkPatchResult(BaseModel):
    """`skipped` names the ids the caller may not touch rather than refusing
    the whole selection: a selection made from the board can include a row
    that vanished or was never theirs, and failing everything for one would
    send the page back to a request per row.
    """

    ok: bool
    updated: int
    skipped: list[int]


class Deleted(BaseModel):
    ok: bool


def _patch_fields(body: UserJobPatch) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    return fields


@patch_router.patch("/user/jobs/{job_id}", response_model_exclude_none=True)
def patch_job(
    job_id: int, body: UserJobPatch, user: AuthedUser = Depends(require_user)
) -> PatchResult:
    if job_id not in touchable_job_ids(user.id, [job_id]):
        raise refuse(404, "NOT_FOUND", "unknown job")
    fields = _patch_fields(body)
    return PatchResult(ok=True, autofilled=Autofilled(**write_board_row(user.id, job_id, fields)))


@patch_router.patch("/user/jobs")
def patch_jobs(
    body: UserJobsBulkPatch, user: AuthedUser = Depends(require_user)
) -> BulkPatchResult:
    """One patch across a selection, so a 6,000-row selection is one request
    rather than 6,000. Ids the caller may not touch are skipped and named,
    not refused whole: a selection made from the board can include a row that
    vanished or was never theirs, and failing everything for it would send
    the page back to one request per row."""
    fields = _patch_fields(body.patch)
    allowed = touchable_job_ids(user.id, body.job_ids)
    changed = [j for j in body.job_ids if j in allowed]
    for job_id in changed:
        write_board_row(user.id, job_id, fields, publish=False)
    # One event for the whole selection, off the per-row path.
    events.publish_board_rows(user.id, changed, fields)
    updated = len(changed)
    return BulkPatchResult(
        ok=True,
        updated=updated,
        skipped=[j for j in body.job_ids if j not in allowed],
    )


@delete_router.delete("/user/jobs/{job_id}")
def delete_user_job(job_id: int, user: AuthedUser = Depends(require_user)) -> Deleted:
    """Drops the user's board row only (the catalog job is untouched); a later
    run re-materializes it if it still passes their filters. Hide is the
    permanent alternative."""
    db.execute("DELETE FROM user_jobs WHERE user_id = %s AND job_id = %s", (user.id, job_id))
    return Deleted(ok=True)


@delete_router.delete("/user/jobs")
def delete_user_jobs(
    body: UserJobsBulkIds, user: AuthedUser = Depends(require_user)
) -> BulkDeleted:
    """The selection form of the delete above: the caller's own rows only,
    one statement, count returned."""
    deleted = db.execute_count(
        "DELETE FROM user_jobs WHERE user_id = %s AND job_id = ANY(%s)", (user.id, body.job_ids)
    )
    return BulkDeleted(ok=True, deleted=deleted)
