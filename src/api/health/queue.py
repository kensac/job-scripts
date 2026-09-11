"""The queue: what is waiting, and what is wedged."""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.health.evidence import WORKER_FRESH
from api.queue import INGEST_INTERVAL_MINUTES

logger = logging.getLogger(__name__)


def _detect_queue() -> list[dict[str, Any]]:
    """The queue not moving when it should.

    Two shapes, each invisible from the totals on the dashboard: a worker
    reporting idle while pending work sits there (a claim takes one poll, so
    minutes of that is a stall, or a kinds allowlist that excludes what is
    queued), and pending ingests older than the cycle allows (the fleet is
    behind the hour and boards are going stale). Thresholds are persisted
    config, so an operator tunes them without a deploy.
    """
    found: list[dict[str, Any]] = []
    stall_minutes = int(db.get_config("queue_stall_minutes"))
    for r in db.query(
        """
        SELECT w.name, w.last_seen, o.n AS pending, o.kinds,
               EXTRACT(EPOCH FROM now() - o.at) / 60 AS oldest_minutes
        FROM worker_status w
        CROSS JOIN LATERAL (
            -- Per worker, not fleet-wide: a host that refuses ingest is not
            -- stalled by a queue full of ingest. The filters mirror
            -- _claim_task's, so this counts exactly what that worker would
            -- have taken had it been able to.
            SELECT MIN(t.created_at) AS at, COUNT(*) AS n,
                   array_agg(DISTINCT t.kind ORDER BY t.kind) AS kinds
            FROM tasks t
            WHERE t.status = 'pending'
              AND (cardinality(w.kinds) = 0 OR t.kind = ANY(w.kinds))
              AND NOT (t.kind = ANY(w.excluded_kinds))
              -- What the claim would refuse this worker is not work it could
              -- have taken: a pull waiting for its host's slot is not a stall.
              AND (t.not_before IS NULL OR t.not_before <= now())
              AND NOT EXISTS (
                  SELECT 1 FROM host_budget b
                  WHERE b.host = t.payload->>'host'
                    AND b.egress_group = COALESCE(w.egress_group, w.name)
                    AND b.next_allowed_at > now())
        ) o
        WHERE w.current_task_id IS NULL
          AND w.last_seen > now() - %(fresh)s::interval
          AND o.at < now() - make_interval(mins => %(stall)s)
        """,
        {"fresh": WORKER_FRESH, "stall": stall_minutes},
    ):
        found.append(
            {
                "kind": "queue_stalled",
                "subject": r["name"],
                "severity": "critical",
                "message": (
                    f"{r['name']} has reported idle while {r['pending']} tasks sit pending, the "
                    f"oldest for {r['oldest_minutes']:.0f} minutes ({', '.join(r['kinds'])}). A "
                    f"claim takes one poll, and this count already excludes kinds this "
                    "worker refuses, so it cannot be explained by its kind filters."
                ),
                "detail": {
                    "worker": r["name"],
                    "pending": r["pending"],
                    "kinds": r["kinds"],
                    "oldest_minutes": round(float(r["oldest_minutes"]), 1),
                    "stall_minutes": stall_minutes,
                },
            }
        )

    cycles = int(db.get_config("ingest_backlog_cycles"))
    limit_minutes = cycles * INGEST_INTERVAL_MINUTES
    r = db.query_one(
        """
        SELECT COUNT(*) AS pending,
               EXTRACT(EPOCH FROM now() - MIN(created_at)) / 60 AS oldest_minutes,
               (SELECT COUNT(*) FROM tasks t2 WHERE t2.kind = 'ingest_source'
                  AND t2.finished_at > now() - interval '1 hour') AS done_last_hour
        FROM tasks WHERE kind = 'ingest_source' AND status = 'pending'
        """
    )
    if r and r["pending"] and float(r["oldest_minutes"] or 0) > limit_minutes:
        found.append(
            {
                "kind": "ingest_backlog",
                "subject": "ingest_source",
                "severity": "warning",
                "message": (
                    f"{r['pending']} ingests are pending and the oldest has waited "
                    f"{r['oldest_minutes']:.0f} minutes, past {cycles} cycles of "
                    f"{INGEST_INTERVAL_MINUTES}. The fleet finished {r['done_last_hour']} in the "
                    "last hour; at that rate the pile is what the number says it is."
                ),
                "detail": {
                    "pending": r["pending"],
                    "oldest_minutes": round(float(r["oldest_minutes"]), 1),
                    "done_last_hour": r["done_last_hour"],
                    "limit_minutes": limit_minutes,
                },
            }
        )
    return found


# How many consecutive failed ingests mean a board is broken rather than
# unlucky. Measured over the week to 2026-09-04: no source failed more than
# once, and every failure was transient (a closed connection during a roll,
