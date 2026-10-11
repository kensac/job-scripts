"""A person's filters, run controls, presets and prompt assistance."""

from fastapi import APIRouter

from api.routers import filter_crud, filter_presets, filter_prompt, filter_run_routes
from api.routers.filter_prompt import IMPROVE_MODEL as IMPROVE_MODEL
from api.routers.filter_run_routes import DEFERRED_MESSAGE as DEFERRED_MESSAGE
from api.routers.filter_run_routes import REJUDGE_GROUPS_KEY as REJUDGE_GROUPS_KEY
from api.routers.filter_run_routes import _rejudge_on_change as _rejudge_on_change

router = APIRouter()
router.include_router(filter_crud.router)
router.include_router(filter_run_routes.router)
router.include_router(filter_presets.router)
router.include_router(filter_prompt.router)
