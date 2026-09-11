"""May this signed-in user address this job, and what happens when they may not.

Not in routers/jobs.py, which is where this lived while routers/application.py
and routers/requirements.py imported it from there. A gate three routers share
is not one router's private helper, and importing it from a sibling router
pulls that whole module in to get one function.

Not in api/visibility.py either, though that owns the predicate underneath.
The task runtime imports visibility (tasks/board.py runs the recompute), and
the 404 below is a FastAPI concern, so putting it there would put the web
framework in the worker's import path.

The raise stays attached to the read. Which status a refusal gets is part of
this gate's contract rather than a caller's decision, and a caller free to
forget it is how the hole the docstring below describes was opened.
"""

from __future__ import annotations

from fastapi import HTTPException

from api import db
from api.auth import AuthedUser
from api.board import visibility


def _visible_job(user: AuthedUser, job_id: int, columns: str) -> dict | None:
    """One job, but only if this user may address it.

    Every per-job route needs this and none of them had it: they resolved the
    job with a bare `WHERE id = %s`, so any signed-in user could name any of
    the 49k job ids. That let them read another user's private upload, pin it
    to their own board, and - through the explain route, which writes a verdict
    into an append-only log with no user_id - flip a job's closed status for
    EVERY user at once, because latest-row-per-(url, check_type) wins globally.

    The gate is the board's own membership (api.board.visibility.FAST) rather than
    a new predicate. A fourth spelling of "can this user see this job" is how
    the first three drifted.
    """
    return db.query_one(
        visibility.FAST.format(columns=columns, extra="AND j.id = %(jid)s"),
        {"uid": user.id, "jid": job_id},
    )


def require_visible_job(user: AuthedUser, job_id: int, columns: str) -> dict:
    job = _visible_job(user, job_id, columns)
    if not job:
        # 404, not 403: whether a job exists is itself information the caller
        # is not entitled to.
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    return job
