"""User routes assembled from account, model catalog, and settings domains."""

from fastapi import APIRouter

from api import budget as budget
from api.routers.users_routes import account, model_catalog, settings
from api.routers.users_routes.model_catalog import (  # re-exported for existing callers
    NEEDS_OWN_KEY as NEEDS_OWN_KEY,
)
from api.routers.users_routes.model_catalog import (
    NOT_ON_ALLOWLIST as NOT_ON_ALLOWLIST,
)
from api.routers.users_routes.model_catalog import (
    _catalog as _catalog,
)
from api.routers.users_routes.model_catalog import (
    _provider_params as _provider_params,
)

router = APIRouter()
router.include_router(account.router)
router.include_router(model_catalog.router)
router.include_router(settings.router)
