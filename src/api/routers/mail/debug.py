"""Administrative mail inspection, distributions, and reversible corrections.

Classification is paused by configuration. These routes inspect recorded facts
and accept explicit administrator corrections; importing them starts no work.
"""

from fastapi import APIRouter

from api.routers.mail import debug_actions, debug_analytics, debug_inspection
from api.routers.mail.debug_inspection import NEVER_ATTEMPTED as NEVER_ATTEMPTED

router = APIRouter()
router.include_router(debug_inspection.list_router)
router.include_router(debug_analytics.router)
router.include_router(debug_inspection.detail_router)
router.include_router(debug_actions.router)
