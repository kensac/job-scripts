"""The fleet itself: who is running what."""

from __future__ import annotations

import logging
from typing import Any

from api import db, telemetry
from api.health.evidence import WORKER_FRESH

logger = logging.getLogger(__name__)


def _detect_fleet() -> list[dict[str, Any]]:
    return _mixed_release() + _telemetry_failing()


def _mixed_release() -> list[dict[str, Any]]:
    """A live worker on a different release from the api.

    A roll recreates every container from one image; a host whose deploy
    never ran keeps heartbeating on the old one, with old code against the
    migrated database, and looks healthy on every other count. The api's
    own release is the reference because it rolls first. A worker whose
    process started before the api's own release was built is allowed the
    roll's own duration; past fleet_roll_minutes it is a host that did not
    deploy. Unknown releases (a local build) are not compared.
    """

    if telemetry.RELEASE == "unknown":
        return []
    roll_minutes = int(db.get_config("fleet_roll_minutes"))
    return [
        {
            "kind": "fleet_mixed_release",
            "subject": r["name"],
            "severity": "critical",
            "message": (
                f"{r['name']} is running {r['release'] or 'an unknown release'} while the api "
                f"runs {telemetry.RELEASE}, and has been heartbeating on it for "
                f"{r['minutes']:.0f} minutes. A roll takes under {roll_minutes}; this host did "
                "not deploy, and its code is older than the database it is writing to."
            ),
            "detail": {
                "worker": r["name"],
                "worker_release": r["release"],
                "api_release": telemetry.RELEASE,
                "minutes": round(float(r["minutes"]), 1),
            },
        }
        for r in db.query(
            """
            SELECT name, release, EXTRACT(EPOCH FROM now() - started_at) / 60 AS minutes
            FROM worker_status
            WHERE last_seen > now() - %(fresh)s::interval
              AND release IS DISTINCT FROM %(api)s
              AND started_at < now() - make_interval(mins => %(roll)s)
            ORDER BY name
            """,
            {"fresh": WORKER_FRESH, "api": telemetry.RELEASE, "roll": roll_minutes},
        )
    ]


def _telemetry_failing() -> list[dict[str, Any]]:
    """A live worker whose OTLP exporter is failing now and has delivered
    nothing for as long.

    The telemetry layer never raises, so a failing exporter only counted
    itself in jobtracker_telemetry_exports_total, and no alert rule read it.
    Both bounds are WORKER_FRESH: the latest failure is inside it, so an idle
    worker with an old failure and nothing since to send is not called
    failing; and the latest success, or the process start, is before it, so
    one failed batch between two good ones is not either. The batch
    processors export every few seconds while there is anything to send.

    Blind to the other way telemetry goes missing: PostHog's OTLP endpoint
    answers 200 and drops the records. That is a success here, and on
    2026-10-31 it held from 18:34 to 00:20 UTC for every service in the
    project, this fleet's exporters counting every batch as ok throughout.
    """
    return [
        {
            "kind": "telemetry_export_failing",
            "subject": r["name"],
            "severity": "warning",
            "message": (
                f"{r['name']} has not delivered a log or trace batch to PostHog for "
                f"{r['minutes']:.0f} minutes, and its latest export failed. Its logs and "
                "traces are being lost; errors and events take another path. Its stdout "
                "log carries the exporter's reason."
            ),
            "detail": {
                "worker": r["name"],
                "last_ok_at": r["telemetry_ok_at"].isoformat() if r["telemetry_ok_at"] else None,
                "last_failed_at": r["telemetry_failed_at"].isoformat(),
                "minutes": round(float(r["minutes"]), 1),
            },
        }
        for r in db.query(
            """
            SELECT name, telemetry_ok_at, telemetry_failed_at,
                   EXTRACT(EPOCH FROM now() - COALESCE(telemetry_ok_at, started_at)) / 60
                       AS minutes
            FROM worker_status
            WHERE last_seen > now() - %(fresh)s::interval
              AND telemetry_failed_at > now() - %(fresh)s::interval
              AND COALESCE(telemetry_ok_at, started_at) < now() - %(fresh)s::interval
            ORDER BY name
            """,
            {"fresh": WORKER_FRESH},
        )
    ]
