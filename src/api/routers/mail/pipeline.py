"""A person's derived pipeline views and explicit corrections."""

from fastapi import APIRouter

from api.routers.mail import pipeline_actions, pipeline_views

router = APIRouter()
router.include_router(pipeline_views.router)
router.include_router(pipeline_actions.router)
