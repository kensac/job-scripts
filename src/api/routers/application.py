"""A person's resumes and one visible job's application form."""

from fastapi import APIRouter

from api.routers import application_answers, application_resumes

router = APIRouter()
router.include_router(application_resumes.router)
router.include_router(application_answers.router)
