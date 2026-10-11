"""Mail inspection and correction routes.

The admin views expose decisions for filtering and drill-down. Corrections
append a newer classification or match rather than editing the earlier row.
This package assembles the debug, pipeline, message, and action routers;
`api.mail` provides the services they call.
"""

from fastapi import APIRouter

from api.routers.mail import actions, debug, messages, pipeline

router = APIRouter()
router.include_router(debug.router)
router.include_router(pipeline.router)
router.include_router(messages.router)
router.include_router(actions.router)
