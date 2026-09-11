"""The mail pipeline, visible enough to correct.

Mirrors /admin/queries deliberately: filter, sort, drill in, see what the model
actually said. That surface is the one people trust in this codebase, and a
pipeline whose decisions cannot be inspected is one whose mistakes are found
by noticing a wrong answer months later.

Every decision here is reversible by design, which is what makes an override
endpoint honest rather than a patch: classifications and matches are both
append-only, so a correction is a newer row, not an edit.

This was one 2,119 line module answering four different questions. Each is now
a module named for the question its handlers answer, and the router the
application includes is assembled from theirs here. `api.mail`, imported all
through them, is a different package: the services this layer calls.
"""

from fastapi import APIRouter

from api.routers.mail import actions, debug, messages, pipeline

router = APIRouter()
router.include_router(debug.router)
router.include_router(pipeline.router)
router.include_router(messages.router)
router.include_router(actions.router)
