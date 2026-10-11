"""A person's filters, run controls, presets and prompt assistance."""

from fastapi import APIRouter

from api.routers.filtering import presets, prompt_assistance, run_controls, saved_filters
from api.routers.filtering.prompt_assistance import IMPROVE_MODEL as IMPROVE_MODEL
from api.routers.filtering.run_controls import DEFERRED_MESSAGE as DEFERRED_MESSAGE
from api.routers.filtering.run_controls import REJUDGE_GROUPS_KEY as REJUDGE_GROUPS_KEY
from api.routers.filtering.run_controls import _rejudge_on_change as _rejudge_on_change

router = APIRouter()
router.include_router(saved_filters.router)
router.include_router(run_controls.router)
router.include_router(presets.router)
router.include_router(prompt_assistance.router)
