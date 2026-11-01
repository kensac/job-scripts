"""User routes assembled from account, model catalog, and settings domains."""

from fastapi import APIRouter

from api.routers.users_routes import account, model_catalog, settings

router = APIRouter()
router.include_router(account.router)
router.include_router(model_catalog.router)
router.include_router(settings.router)
