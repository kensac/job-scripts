"""The parts more than one of these surfaces needs, spelled once.

The gate is here because every module in this package depends on it, and ten
routers outside it import it from the package. The summary window is here
because the queue and the ingest summaries agree on it and neither owns it.
"""

from __future__ import annotations

import os

from fastapi import Depends, HTTPException

from api.auth import AuthedUser, require_user

ADMIN_GROUPS = {
    g.strip()
    for g in os.environ.get("JOBTRACKER_ADMIN_GROUPS", "infra-admins").split(",")
    if g.strip()
}


def require_admin(user: AuthedUser = Depends(require_user)) -> AuthedUser:
    if not ADMIN_GROUPS.intersection(user.groups):
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "admin group required"})
    return user


# The longest window the queue and ingest summaries will aggregate. A week is
# what the health detectors baseline against; anything longer is a report,
# not monitoring, and the per-hour series would stop fitting a screen.
SUMMARY_MAX_HOURS = 24 * 7
