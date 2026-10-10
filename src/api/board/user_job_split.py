"""Admission of the legacy user_jobs split backfill (tasks.user_job_backfill).

One admission path for the admin route and the scheduler. The scheduler admits
every cycle until a run has completed, so the deployed fleet runs the backfill
by itself; a run already pending or running is returned, not duplicated.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from api import db, health, queue, telemetry
from api.board.person_state import (
    USER_JOB_SPLIT_CHECKPOINT,
    USER_JOB_SPLIT_DEDUPE_PREFIX,
    USER_JOB_SPLIT_VERSION,
)

KIND = "backfill_user_job_split"
_LOCK = 8_552_021


class NoEligibleWorker(Exception):
    """No fresh worker on this release may claim the split."""


@dataclass(frozen=True)
class Admission:
    task_id: int
    status: str


@dataclass(frozen=True)
class _ExistingSplit:
    id: int
    status: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _SplitBoundary:
    legacy_before: datetime.datetime
    total: int


@dataclass(frozen=True)
class _EligibleWorker:
    name: str


def completed() -> bool:
    return (
        db.query_one("SELECT 1 FROM tasks WHERE kind = %s AND status = 'done' LIMIT 1", (KIND,))
        is not None
    )


def admit() -> Admission:
    """Return the live or finished split, or queue the next generation of one.

    A failed or cancelled run continues from its checkpoint and keeps its
    cutoff, so a resumed split classifies exactly the rows the first one would.
    """
    with db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
        existing = db.query_one_as(
            _ExistingSplit,
            "SELECT id, status, payload FROM tasks WHERE kind = %s "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE",
            (KIND,),
        )
        if existing and existing.status not in ("failed", "cancelled"):
            return Admission(task_id=existing.id, status=existing.status)
        eligible = db.query_one_as(
            _EligibleWorker,
            "SELECT name FROM worker_status "
            "WHERE last_seen > now() - %(fresh)s::interval "
            "AND release IS NOT DISTINCT FROM %(release)s "
            "AND (cardinality(kinds) = 0 OR %(kind)s = ANY(kinds)) "
            "AND NOT (%(kind)s = ANY(excluded_kinds)) "
            "ORDER BY name LIMIT 1",
            {"fresh": health.WORKER_FRESH, "release": telemetry.RELEASE, "kind": KIND},
        )
        if eligible is None:
            raise NoEligibleWorker
        generation = int(existing.payload.get("generation", 0) if existing else 0) + 1
        if existing:
            legacy_before = existing.payload["legacy_before"]
            total = existing.payload["total"]
        else:
            boundary = db.query_one_as(
                _SplitBoundary,
                "WITH boundary AS (SELECT now() AS legacy_before) "
                "SELECT legacy_before, "
                "(SELECT count(*) FROM user_jobs WHERE created_at < legacy_before) AS total "
                "FROM boundary",
            )
            assert boundary is not None
            legacy_before = boundary.legacy_before.isoformat()
            total = boundary.total
        task_id = queue.enqueue(
            KIND,
            {
                "version": USER_JOB_SPLIT_VERSION,
                "generation": generation,
                "legacy_before": legacy_before,
                "total": total,
                **(
                    {USER_JOB_SPLIT_CHECKPOINT: existing.payload[USER_JOB_SPLIT_CHECKPOINT]}
                    if existing and USER_JOB_SPLIT_CHECKPOINT in existing.payload
                    else {}
                ),
            },
            dedupe_key=f"{USER_JOB_SPLIT_DEDUPE_PREFIX}:g{generation}",
        )
        assert task_id is not None
    return Admission(task_id=task_id, status="pending")
