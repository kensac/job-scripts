"""The fleet itself: who is running what."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from api import db
from api.health.evidence import WORKER_FRESH

logger = logging.getLogger(__name__)


def _detect_fleet() -> list[dict[str, Any]]:
    """A live worker on a different release from most of the fleet.

    A roll recreates a host's containers from one image; a host whose pin
    stops following keeps heartbeating on its old one and looks healthy on
    every other count (gcp-vps, an hour behind, 2026-09-04). Only workers
    that have run longer than fleet_roll_minutes are compared, since hosts
    roll one at a time. The reference is the release most of those run.

    It was the release of whichever worker ran this check. data_health is a
    queued task, so a worker one Renovate bump behind flagged hetzner, one
    bump ahead, as "did not deploy": 33 alerts in ten days to 2026-11-21,
    every one cleared by the next roll. A commit cannot be ordered here, so
    the message names both releases and claims neither is older.
    """
    roll_minutes = int(db.get_config("fleet_roll_minutes"))
    settled = db.query(
        """
        SELECT name, release, EXTRACT(EPOCH FROM now() - started_at) / 60 AS minutes
        FROM worker_status
        WHERE last_seen > now() - %(fresh)s::interval
          AND started_at < now() - make_interval(mins => %(roll)s)
          AND release IS DISTINCT FROM 'unknown'
        ORDER BY name
        """,
        {"fresh": WORKER_FRESH, "roll": roll_minutes},
    )
    counts = Counter(r["release"] for r in settled if r["release"])
    if not counts:
        return []
    # ponytail: a tie picks the first counted; with an even split the alert
    # names one side, and either side is a pin worth reading.
    common, peers = counts.most_common(1)[0]
    return [
        {
            "kind": "fleet_mixed_release",
            "subject": r["name"],
            "severity": "critical",
            "message": (
                f"{r['name']} has run {r['release'] or 'an unknown release'} for "
                f"{r['minutes']:.0f} minutes while {peers} other worker(s) run {common}. "
                f"Hosts roll within {roll_minutes} minutes of each other, so this host's pin "
                "is not following the rest of the fleet."
            ),
            "detail": {
                "worker": r["name"],
                "worker_release": r["release"],
                "fleet_release": common,
                "minutes": round(float(r["minutes"]), 1),
            },
        }
        for r in settled
        if r["release"] != common
    ]
