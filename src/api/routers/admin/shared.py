"""The parts more than one of these surfaces needs, spelled once.

The gate is here because every module in this package depends on it, and ten
routers outside it import it from the package. The summary window is here
because the queue and the ingest summaries agree on it and neither owns it.
"""

from __future__ import annotations

from fastapi import Depends

from api.auth import AuthedUser, is_admin, require_user
from api.problem import refuse


def require_admin(user: AuthedUser = Depends(require_user)) -> AuthedUser:
    if not is_admin(user.groups):
        raise refuse(403, "FORBIDDEN", "admin group required")
    return user


# The longest window the queue and ingest summaries will aggregate. A week is
# what the health detectors baseline against; anything longer is a report,
# not monitoring, and the per-hour series would stop fitting a screen.
SUMMARY_MAX_HOURS = 24 * 7
