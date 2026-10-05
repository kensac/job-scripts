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

import datetime
from typing import Any

from api import db, events


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


# What a pull of a source that is off fails with. The task never asked the
# board, so it is not a failed pull and does not count toward a run.
INACTIVE_SOURCE_ERROR = "unknown or inactive source"

# The run of failed pulls since the source's last successful one, read off the
# tasks that already record every pull rather than kept as state beside them.
# It walks idx_tasks_ingest_source_latest: one probe for the last success, one
# range after it. Over all 8,021 sources it took 0.68 s on production
# (2026-10-04), which is why the scheduler asks it only of the sources due.
_FAILURE_RUN = f"""
    SELECT count(*) AS failures, max(f.created_at) AS last_failed,
           (array_agg(f.error ORDER BY f.id DESC))[1] AS last_error
    FROM tasks f
    WHERE f.kind = 'ingest_source' AND f.payload->>'source' = s.name
      AND f.status = 'failed' AND f.error IS DISTINCT FROM '{INACTIVE_SOURCE_ERROR}'
      AND f.id > COALESCE((SELECT max(p.id) FROM tasks p
                           WHERE p.kind = 'ingest_source' AND p.payload->>'source' = s.name
                             AND p.status = 'done'), 0)
"""


def failure_runs(sources: list[str] | None = None, at_least: int = 1) -> dict[str, dict[str, Any]]:
    """Each source's run of failed pulls (failures, last_failed, last_error,
    with the source's active and ingest_interval_hours), for the named sources
    or every source, where the run is at least `at_least` long."""
    rows = db.query(
        f"""
        SELECT s.name, s.active, s.ingest_interval_hours, run.*
        FROM sources s CROSS JOIN LATERAL ({_FAILURE_RUN}) run
        WHERE (%(names)s::text[] IS NULL OR s.name = ANY(%(names)s)) AND run.failures >= %(at_least)s
        """,
        {"names": sources, "at_least": at_least},
    )
    return {r["name"]: r for r in rows}


def pull_wait_hours(interval_hours: int, failures: int) -> int:
    """How long after its latest failed pull a board waits: its own interval,
    doubled for each further failure in the run, capped at
    ingest_retry_max_hours but never below the interval. So a failure counts
    toward the interval as a success does, and a run backs off from there."""
    cap = int(db.get_config("ingest_retry_max_hours"))
    return max(interval_hours, min(interval_hours * 2 ** (failures - 1), cap))


def backing_off(sources: list[str]) -> set[str]:
    """The sources whose run of failed pulls says to wait. A run that reached
    ingest_give_up_after_failures does not wait: its next pull either succeeds
    or switches the source off (tasks.ingest). That is also what a source
    switched back on by hand gets: one more pull, now."""
    if not sources:
        return set()
    give_up = int(db.get_config("ingest_give_up_after_failures"))
    now = datetime.datetime.now(datetime.UTC)
    return {
        name
        for name, run in failure_runs(sources).items()
        if run["failures"] < give_up
        and run["last_failed"]
        > now
        - datetime.timedelta(hours=pull_wait_hours(run["ingest_interval_hours"], run["failures"]))
    }
