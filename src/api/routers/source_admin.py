"""Admin source catalog routes, grouped by read and write responsibility."""

from fastapi import APIRouter

from api.routers.source_management import bulk_switch, catalog, listing, title_screening
from api.routers.source_management.listing import SourceLedger as SourceLedger
from api.routers.source_management.listing import SourceName as SourceName

router = APIRouter(prefix="/admin")
router.include_router(listing.router)
router.include_router(catalog.router)
router.include_router(bulk_switch.router)
router.include_router(title_screening.router)
