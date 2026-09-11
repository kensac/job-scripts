"""The fleet itself: who is running what."""

from __future__ import annotations

import logging
from typing import Any

from api import db, telemetry
from api.health.evidence import WORKER_FRESH

logger = logging.getLogger(__name__)


def _detect_fleet() -> list[dict[str, Any]]:
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
