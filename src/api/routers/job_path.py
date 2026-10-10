"""Why one posting is or is not on this person's board, stage by stage."""

from fastapi import APIRouter, Depends

from api import posting_path
from api.auth import AuthedUser, require_user
from api.board.access import require_visible_job
from api.problem import refuse

router = APIRouter()


@router.get("/user/jobs/{job_id}/path")
def own_posting_path(
    job_id: int, user: AuthedUser = Depends(require_user)
) -> posting_path.PostingPath:
    require_visible_job(user, job_id, "j.id")
    path = posting_path.for_user(job_id, user.id)
    if path is None:
        raise refuse(404, "NOT_FOUND", "unknown job")
    return path
