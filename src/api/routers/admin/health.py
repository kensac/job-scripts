"""What is broken: the open data-health alerts, and the failures behind them."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api import db, health
from api.auth import AuthedUser
from api.routers.admin.shared import require_admin

router = APIRouter()


@router.get("/health")
def data_health(user: AuthedUser = Depends(require_admin)):
    """Open data-health alerts plus recently resolved ones, so an upstream
    break is something you're told about rather than something you discover."""
    return {
        # Nothing is suppressed any more: the detectors exclude backlog-sweep
        # rows individually (health.FRESH_CHECK_WINDOW) instead of switching a
        # whole detector off while the content backfill runs. Kept in the
        # response so a future suppression has somewhere to be reported.
        # A detector that is off and says nothing is indistinguishable from a
        # detector that sees nothing.
        "suppressed": [],
        # subject_kind says WHAT an alert's subject is - a source, a host, a
        # provider and user, a task kind. It is not the same thing across
        # detectors, and the dashboard linked all of them to the sources page,
        # which is correct for two of five. Annotated on read from the one map
        # in health.py rather than stored per row, so alerts already open get
        # the right answer without a backfill.
        "open": [
            {**a, "subject_kind": health.subject_kind_for(a["kind"])}
            for a in db.query(
                "SELECT * FROM health_alerts WHERE resolved_at IS NULL "
                "ORDER BY severity, last_seen DESC"
            )
        ],
        "recently_resolved": [
            {**a, "subject_kind": health.subject_kind_for(a["kind"])}
            for a in db.query(
                "SELECT * FROM health_alerts WHERE resolved_at > now() - interval '7 days' "
                "ORDER BY resolved_at DESC LIMIT 20"
            )
        ],
        "content_mix": db.query(
            """
            SELECT j.source,
                   COUNT(*) FILTER (WHERE q.reason = 'ats text') AS ats_text,
                   COUNT(*) FILTER (WHERE q.reason = 'scraped') AS scraped,
                   COUNT(*) AS total
            FROM ai_queries q JOIN jobs j ON j.url = q.url
            WHERE q.check_type = 'content'
              AND q.created_at > now() - interval '7 days'
            GROUP BY j.source ORDER BY total DESC
            """
        ),
    }


@router.post("/health/check")
async def run_health_check(user: AuthedUser = Depends(require_admin)):
    """Run the detectors now instead of waiting for the hourly task."""
    from api import health

    found = health.detect()
    fresh = health.record(found)
    return {"open": len(found), "new": len(fresh), "alerts": found}


@router.get("/failures")
def failure_breakdown(
    hours: int = 24,
    worker: str | None = None,
    host: str | None = None,
    user: AuthedUser = Depends(require_admin),
):
    """Failed checks pivoted by fleet host and URL host: one worker failing on
    hosts the others handle fine is the signature of an IP block. Pass worker
    and/or host to drill into the individual failures behind a pivot row."""
    hours = max(1, min(hours, 720))
    params: dict = {"hours": hours, "worker": worker, "host": host}
    rows = db.query(
        """
        SELECT COALESCE(worker, 'unknown') AS worker,
               substring(url from '//([^/]+)') AS host,
               check_type, COUNT(*) AS failures,
               MAX(created_at) AS last_failure
        FROM ai_queries
        WHERE status = 'failed'
          AND created_at > now() - make_interval(hours => %(hours)s)
          AND (%(worker)s::text IS NULL OR COALESCE(worker, 'unknown') = %(worker)s)
          AND (%(host)s::text IS NULL OR substring(url from '//([^/]+)') = %(host)s)
        GROUP BY 1, 2, 3 ORDER BY failures DESC LIMIT 100
        """,
        params,
    )
    items = []
    if worker or host:
        items = db.query(
            """
            SELECT id, created_at, url, check_type, company, job_title,
                   COALESCE(worker, 'unknown') AS worker, left(error, 300) AS error, reason
            FROM ai_queries
            WHERE status = 'failed'
              AND created_at > now() - make_interval(hours => %(hours)s)
              AND (%(worker)s::text IS NULL OR COALESCE(worker, 'unknown') = %(worker)s)
              AND (%(host)s::text IS NULL OR substring(url from '//([^/]+)') = %(host)s)
            ORDER BY id DESC LIMIT 200
            """,
            params,
        )
    return {"rows": rows, "items": items}
