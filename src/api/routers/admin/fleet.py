"""Admin fleet routes, ordered by task, batch, and worker views."""

from fastapi import APIRouter

from api.routers.admin import fleet_batches, fleet_tasks, fleet_workers

router = APIRouter()
router.include_router(fleet_tasks.router)
router.include_router(fleet_batches.listing_router)
router.include_router(fleet_workers.router)
router.include_router(fleet_batches.detail_router)
