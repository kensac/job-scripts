"""Admin verdict ledger routes in public registration order."""

from fastapi import APIRouter

from api.routers.admin import ledger_stats, posting_inspection, query_ledger

router = APIRouter()
router.include_router(query_ledger.router)
router.include_router(posting_inspection.router)
router.include_router(ledger_stats.router)
