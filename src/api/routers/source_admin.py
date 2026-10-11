"""Admin source catalog routes, grouped by read and write responsibility."""

from fastapi import APIRouter

from api.routers import (
    source_admin_catalog,
    source_admin_listing,
    source_admin_patterns,
    source_admin_switch,
)
from api.routers.source_admin_listing import SourceLedger as SourceLedger
from api.routers.source_admin_listing import SourceName as SourceName

router = APIRouter(prefix="/admin")
router.include_router(source_admin_listing.router)
router.include_router(source_admin_catalog.router)
router.include_router(source_admin_switch.router)
router.include_router(source_admin_patterns.router)
