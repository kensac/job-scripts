from fastapi import APIRouter, Depends, Query

from api import pagination
from api.auth import AuthedUser, require_user
from api.board.access import require_visible_job
from api.review_gate_reads import ReviewDecisions, read_decisions

router = APIRouter()


@router.get("/user/jobs/{job_id}/review-decisions")
def own_review_decisions(
    job_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: AuthedUser = Depends(require_user),
) -> ReviewDecisions:
    job = require_visible_job(user, job_id, "j.id,j.url")
    return read_decisions(
        "d.url=%(url)s AND d.user_id=%(uid)s AND d.managed_board_id IS NULL",
        {"url": job["url"], "uid": user.id},
        pagination.Page.from_params(page, page_size, maximum=100),
        {},
        personal=True,
    )
