"""A person's filters, run controls, presets and prompt assistance."""

from fastapi import APIRouter

from api.routers.filtering import presets, prompt_assistance, run_controls, saved_filters

router = APIRouter()
router.include_router(saved_filters.router)
router.include_router(run_controls.router)
router.include_router(presets.router)
router.include_router(prompt_assistance.router)
