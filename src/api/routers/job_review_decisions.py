from fastapi import APIRouter, Depends

from api.auth import AuthedUser, require_user
from api.board.access import require_visible_job
from api.routers.admin.review_gates import ReviewDecisions

router = APIRouter()


@router.get("/user/jobs/{job_id}/review-decisions")
def own_review_decisions(job_id: int, user: AuthedUser = Depends(require_user)) -> ReviewDecisions:
    require_visible_job(user, job_id, "j.id")
    return ReviewDecisions()
