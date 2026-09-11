"""Putting work on the queue, and the cadence the queue runs at.

BELOW the handlers on purpose. tasks/runtime/ is the runtime a handler
runs inside: claims, heartbeats, progress, batches. This is the smaller thing
that code outside the task system needs, and needing it is not a reason to
reach into the handlers package.

Three modules were reaching. api/visibility.py asks for a board recompute,
api/health.py measures ingest lateness in multiples of the interval, and
api/worker.py buckets time by it. Two of them imported inside a function to
dodge the import cycle that reaching created, which is the shape of a
dependency pointing the wrong way.
"""

from __future__ import annotations

import os
from typing import Any

from api import db, events

# api/worker.py buckets time by it; the ingest_backlog detector in
# api/health.py measures lateness in multiples of it. Lives here so health
# never imports the worker.
INGEST_INTERVAL_MINUTES = int(os.environ.get("JOBTRACKER_INGEST_INTERVAL_MINUTES", "60"))


def enqueue(kind: str, payload: dict[str, Any], dedupe_key: str | None = None) -> int | None:
    """Insert a task; with a dedupe_key, at most one task per key ever exists,
    so every fleet worker can race to enqueue and exactly one wins.

    parent_id is mirrored out of the payload into its own column: it is the
    only payload field that gets queried, and no index can serve
    payload->>'parent_id'. It stays in the payload too so a chunk handler
    reading its own payload is unchanged."""
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, dedupe_key, parent_id) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
        (kind, db.jsonb(payload), dedupe_key, payload.get("parent_id")),
    )
    if row:
        events.publish_task(row["id"])
    return row["id"] if row else None
