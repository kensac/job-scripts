"""Data-health detection: what is wrong right now, and the record of it.

This was one 1,159 line module holding five detectors, a vocabulary and a
recorder. It is a package because the five detectors are five subjects, not
five sections: each owns its own alert kinds and its own reason for existing,
and `detect` already ran each in isolation so that one raising did not take
the others down.

Everything the old module exposed is re-exported here, so `from api import
health` and `health.detect()` read exactly as they did.
"""

from __future__ import annotations

import logging
from typing import Any

from api import db, telemetry
from api.health.boards import _detect_boards
from api.health.evidence import (
    FRESH_CHECK_WINDOW as FRESH_CHECK_WINDOW,
)
from api.health.evidence import (
    MAX_PER_COMPANY as MAX_PER_COMPANY,
)
from api.health.evidence import (
    MIN_CONTENT_SAMPLES as MIN_CONTENT_SAMPLES,
)
from api.health.evidence import (
    MIN_SAMPLES as MIN_SAMPLES,
)
from api.health.evidence import (
    WORKER_FRESH as WORKER_FRESH,
)
from api.health.evidence import (
    _int_from as _int_from,
)
from api.health.evidence import (
    _pct as _pct,
)
from api.health.fleet import _detect_fleet
from api.health.queue import _detect_queue
from api.health.silent import SWEEP_KINDS as SWEEP_KINDS
from api.health.silent import _detect_silent
from api.health.sources import _detect_sources
from api.health.subjects import (
    SUBJECT_ATS as SUBJECT_ATS,
)
from api.health.subjects import (
    SUBJECT_DETECTOR as SUBJECT_DETECTOR,
)
from api.health.subjects import (
    SUBJECT_HOST as SUBJECT_HOST,
)
from api.health.subjects import (
    SUBJECT_PROVIDER_USER as SUBJECT_PROVIDER_USER,
)
from api.health.subjects import (
    SUBJECT_PURPOSE as SUBJECT_PURPOSE,
)
from api.health.subjects import (
    SUBJECT_SOURCE as SUBJECT_SOURCE,
)
from api.health.subjects import (
    SUBJECT_TASK as SUBJECT_TASK,
)
from api.health.subjects import (
    SUBJECT_TASK_KIND as SUBJECT_TASK_KIND,
)
from api.health.subjects import (
    SUBJECT_WORKER as SUBJECT_WORKER,
)
from api.health.subjects import (
    subject_kind_for as subject_kind_for,
)

logger = logging.getLogger(__name__)

# How long a condition must go undetected before it is considered over. A
# detector stops firing when it stops being EVALUABLE (sample count dipped
# under the floor, the 24h window rolled past the event) at least as often as
# when the condition ends; resolving on the first miss makes those alerts
# reopen an hour later and re-notify. Re-firing inside the grace period just
# refreshes the open row, so no second email goes out.
RESOLVE_GRACE = "3 hours"


def detect() -> list[dict[str, Any]]:
    """Compares the last 24h against the preceding week, per source, looking for
    the shapes that mean 'something upstream changed' rather than 'the job
    market moved'. Everything here is deliberately relative to each source's
    own baseline. Absolute thresholds would fire constantly on sources that
    are legitimately mostly-closed or legitimately short."""
    found: list[dict[str, Any]] = []

    # Each section on its own: on 2026-09-04 three detectors failed in three
    # ways on one day, the exception took the whole task down, and the open
    # alerts auto-resolved because nothing re-observed them. A raising
    # detector looked exactly like all clear. Now it is an alert of its own.
    for section in (_detect_sources, _detect_boards, _detect_queue, _detect_fleet, _detect_silent):
        try:
            found.extend(section())
        except Exception as exc:
            logger.exception(f"health detector {section.__name__} raised")
            found.append(
                {
                    "kind": "detector_failed",
                    "subject": section.__name__,
                    "severity": "critical",
                    "message": (
                        f"{section.__name__} raised {type(exc).__name__}: {str(exc)[:200]}. "
                        "Every alert it owns is unobserved until it runs again."
                    ),
                    "detail": {"error": str(exc)[:1000]},
                }
            )
    return found


def record(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upserts open alerts and auto-resolves ones that stopped firing. Returns
    only the newly-opened alerts, so notification never repeats for a condition
    that is merely still true."""
    seen = {(f["kind"], f["subject"]) for f in found}
    fresh: list[dict[str, Any]] = []
    for f in found:
        row = db.query_one(
            """
            INSERT INTO health_alerts (kind, subject, severity, message, detail)
            VALUES (%(kind)s, %(subject)s, %(severity)s, %(message)s, %(detail)s)
            ON CONFLICT (kind, subject) WHERE resolved_at IS NULL
            DO UPDATE SET last_seen = now(), message = EXCLUDED.message,
                          detail = EXCLUDED.detail, severity = EXCLUDED.severity
            RETURNING id, (xmax = 0) AS is_new
            """,
            {
                "kind": f["kind"],
                "subject": f["subject"],
                "severity": f["severity"],
                "message": f["message"],
                "detail": db.jsonb(f["detail"]),
            },
        )
        if row and row["is_new"]:
            fresh.append({**f, "id": row["id"]})
            # The condition's timeline beside the raw failures: a detector
            # opening is an event too, so the error tracker can show what
            # was wrong when the tracebacks started.
            telemetry.capture(
                "alert_opened",
                properties={
                    "kind": f["kind"],
                    "subject": f["subject"],
                    "severity": f["severity"],
                    "alert_id": row["id"],
                },
            )

    # Resolve only after RESOLVE_GRACE of silence. A detector goes quiet when
    # it stops being evaluable as readily as when the condition ends, and
    # resolving on the first miss turns that into an alert that reopens next
    # hour and mails again. last_seen is refreshed by the upsert above, so a
    # condition that is still firing never ages into this.
    open_rows = db.query(
        "SELECT id, kind, subject FROM health_alerts WHERE resolved_at IS NULL "
        "AND last_seen < now() - %s::interval",
        (RESOLVE_GRACE,),
    )
    stale = [r for r in open_rows if (r["kind"], r["subject"]) not in seen]
    if stale:
        db.execute(
            "UPDATE health_alerts SET resolved_at = now() WHERE id = ANY(%s)",
            ([r["id"] for r in stale],),
        )
        for r in stale:
            telemetry.capture(
                "alert_resolved",
                properties={"kind": r["kind"], "subject": r["subject"], "alert_id": r["id"]},
            )
    return fresh
