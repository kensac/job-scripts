"""What is broken: the open data-health alerts, and the failures behind them."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db, health
from api.auth import AuthedUser
from api.routers.admin.shared import require_admin

router = APIRouter()


# Every column of health_alerts, named once. Both reads below returned
# SELECT *, which cannot be typed and drifts from whatever reads it.
_ALERT_COLS = (
    "id, kind, subject, severity, message, detail, first_seen, last_seen, notified_at, resolved_at"
)


class HealthAlert(BaseModel):
    """An open or recently resolved alert.

    `subject_kind` is not a column. It says WHAT the subject is - a source, a
    host, a provider and user, a task kind - and is annotated on read from the
    one map in api.health, so an alert opened before the map existed still
    gets the right answer. It is null for a kind the map does not name.

    `detail` is whatever the detector that raised the alert had to show, so
    its shape is that detector's and is not declared further.
    """

    id: int
    kind: str
    subject: str
    severity: str
    message: str
    detail: dict[str, Any] | None
    first_seen: datetime.datetime
    last_seen: datetime.datetime
    notified_at: datetime.datetime | None
    resolved_at: datetime.datetime | None
    subject_kind: str | None


class ContentMix(BaseModel):
    """Where a source's check text came from over the last week. ATS text is
    free and scraped text is paid for, so a source sliding from one to the
    other is a cost change nothing else reports."""

    source: str
    ats_text: int
    scraped: int
    total: int


class DataHealth(BaseModel):
    """`suppressed` is always empty and is kept deliberately: a detector that
    is off and says nothing is indistinguishable from a detector that sees
    nothing, so a future suppression has somewhere to be reported."""

    suppressed: list[str]
    open: list[HealthAlert]
    recently_resolved: list[HealthAlert]
    content_mix: list[ContentMix]


def _annotated(sql: str) -> list[HealthAlert]:
    return [
        HealthAlert(**a, subject_kind=health.subject_kind_for(a["kind"]))
        for a in db.query(f"SELECT {_ALERT_COLS} FROM health_alerts {sql}")
    ]


@router.get("/health")
def data_health(user: AuthedUser = Depends(require_admin)) -> DataHealth:
    """Open data-health alerts plus recently resolved ones, so an upstream
    break is something you're told about rather than something you discover."""
    return DataHealth(
        suppressed=[],
        open=_annotated("WHERE resolved_at IS NULL ORDER BY severity, last_seen DESC"),
        recently_resolved=_annotated(
            "WHERE resolved_at > now() - interval '7 days' ORDER BY resolved_at DESC LIMIT 20"
        ),
        content_mix=db.query_as(
            ContentMix,
            """
            SELECT j.source,
                   COUNT(*) FILTER (WHERE q.reason = 'ats text') AS ats_text,
                   COUNT(*) FILTER (WHERE q.reason = 'scraped') AS scraped,
                   COUNT(*) AS total
            FROM ai_queries q JOIN jobs j ON j.url = q.url
            WHERE q.check_type = 'content'
              AND q.created_at > now() - interval '7 days'
            GROUP BY j.source ORDER BY total DESC
            """,
        ),
    )


class Finding(BaseModel):
    """What a detector raised, before it was written to a row. The same four
    fields health_alerts stores, with no id: a finding that matches an alert
    already open updates it rather than opening a second."""

    kind: str
    subject: str
    severity: str
    message: str
    # Required, not optional: `health.record` already subscripts it, so a
    # detector that omitted it never reached a row in the first place.
    detail: dict[str, Any] | None


class HealthRun(BaseModel):
    """`open` is everything the detectors found; `new` is how much of it was
    not already open, which is what would have been notified."""

    open: int
    new: int
    alerts: list[Finding]
    failed_detectors: list[str]


@router.post("/health/check")
async def run_health_check(user: AuthedUser = Depends(require_admin)) -> HealthRun:
    """Run the detectors now instead of waiting for the hourly task."""
    from api import health

    run = health.detect()
    fresh = health.record(run)
    return HealthRun(
        open=len(run),
        new=len(fresh),
        alerts=[Finding(**f) for f in run],
        failed_detectors=run.failed_detectors,
    )


class FailurePivot(BaseModel):
    """Failures grouped by the worker that ran them and the host they were
    fetched from. `host` is read off the url, so a row with no url has none."""

    worker: str
    host: str | None
    check_type: str | None
    failures: int
    last_failure: datetime.datetime


class FailedCheck(BaseModel):
    """One failure behind a pivot row. `error` is the first 300 characters:
    a driver traceback is longer than the screen and the cause is at the front."""

    id: int
    created_at: datetime.datetime
    url: str | None
    check_type: str | None
    company: str | None
    job_title: str | None
    worker: str
    error: str | None
    reason: str | None


class FailureBreakdown(BaseModel):
    """`items` is empty unless the request named a worker or a host: the
    pivot is the whole fleet, and the individual failures are a drill-down."""

    rows: list[FailurePivot]
    items: list[FailedCheck]


@router.get("/failures")
def failure_breakdown(
    hours: int = 24,
    worker: str | None = None,
    host: str | None = None,
    user: AuthedUser = Depends(require_admin),
) -> FailureBreakdown:
    """Failed checks pivoted by fleet host and URL host: one worker failing on
    hosts the others handle fine is the signature of an IP block. Pass worker
    and/or host to drill into the individual failures behind a pivot row."""
    hours = max(1, min(hours, 720))
    params: dict = {"hours": hours, "worker": worker, "host": host}
    rows = db.query_as(
        FailurePivot,
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
    items: list[FailedCheck] = []
    if worker or host:
        items = db.query_as(
            FailedCheck,
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
    return FailureBreakdown(rows=rows, items=items)
