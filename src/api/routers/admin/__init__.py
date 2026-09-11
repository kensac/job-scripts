"""Everything an administrator can see and change, a module per subject.

No handler here calls a handler in another module, which is why the subjects
can sit apart at all. What they do share is in `shared`, and the router the
application includes is assembled from theirs here.

Order matters in two places and nowhere else. `queries` registers
`/queries/options` before `/queries/{query_id}`, or the literal is swallowed
by the parameter. And `openapi.json` is keyed in registration order, so moving
an include reorders the committed schema even when no route changes.

`require_admin` is re-exported because ten routers outside this package are
gated by it and import it from here.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.routers.admin import (
    catalog,
    checks,
    config,
    extension,
    filters,
    fleet,
    health,
    people,
    queries,
    sources,
)
from api.routers.admin.shared import ADMIN_GROUPS, require_admin

router = APIRouter(prefix="/admin")

router.include_router(filters.router)
router.include_router(sources.router)
router.include_router(people.router)
router.include_router(fleet.router)
router.include_router(catalog.router)
router.include_router(checks.router)
router.include_router(health.router)
router.include_router(config.router)
router.include_router(queries.router)
router.include_router(extension.router)

__all__ = ["ADMIN_GROUPS", "require_admin", "router"]
