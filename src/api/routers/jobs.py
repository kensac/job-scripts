from fastapi import APIRouter

from api.routers import (
    job_board,
    job_detail,
    job_explain,
    job_reports,
    job_tasks,
    job_uploads,
    person_jobs,
)

router = APIRouter()

router.include_router(job_board.router)
router.include_router(person_jobs.patch_router)
router.include_router(job_detail.router)
router.include_router(job_explain.router)
router.include_router(person_jobs.delete_router)
router.include_router(job_uploads.router)
router.include_router(job_reports.router)
router.include_router(job_tasks.router)
