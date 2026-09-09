from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from api import db, events

AdmissionPolicy = Literal["interactive", "scheduled"]
ACTIVE_STATUSES = ("pending", "running", "awaiting_batch", "waiting")


def conflict(user_id: int, filter_id: int | None, *, policy: AdmissionPolicy) -> dict | None:
    # A scheduled splitter may progress while older chunks wait: candidate
    # selection excludes their URLs. Interactive runs wait for the whole run.
    statuses = ("pending", "running") if policy == "scheduled" else ACTIVE_STATUSES
    return db.query_one(
        """
        SELECT id, kind, status, progress, created_at FROM tasks
        WHERE kind IN ('run_filter', 'run_all_filters') AND status = ANY(%(statuses)s)
          AND (payload->>'user_id')::bigint = %(uid)s
          AND (%(fid)s::bigint IS NULL OR kind = 'run_all_filters'
               OR (payload->>'filter_id')::bigint = %(fid)s)
        ORDER BY id DESC LIMIT 1
        """,
        {"uid": user_id, "fid": filter_id, "statuses": list(statuses)},
    )


def admission(user_id: int, filter_id: int | None) -> dict:
    blocked = conflict(user_id, filter_id, policy="interactive")
    return {
        "allowed": blocked is None,
        "reason": "IN_PROGRESS" if blocked else None,
        "message": "An overlapping filter run is already in progress." if blocked else None,
        "task_id": blocked["id"] if blocked else None,
    }


@dataclass(frozen=True)
class RunAdmission:
    task_id: int | None
    conflict: dict | None = None


def enqueue(
    user_id: int,
    filter_id: int | None,
    *,
    policy: AdmissionPolicy,
    ignore_budget: bool = False,
    dedupe_key: str | None = None,
) -> RunAdmission:
    payload = {"user_id": user_id, "ignore_budget": ignore_budget}
    if filter_id is not None:
        payload["filter_id"] = filter_id
    if policy == "scheduled":
        payload["batched"] = True
    with db.transaction():
        # A user row exists even when no task does. Locking it makes checking
        # and enqueueing serial across API processes and scheduling workers.
        db.query_one("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
        blocked = conflict(user_id, filter_id, policy=policy)
        if blocked:
            return RunAdmission(None, blocked)
        row = db.query_one(
            "INSERT INTO tasks (kind, payload, dedupe_key) VALUES (%s, %s, %s) "
            "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
            (
                "run_all_filters" if filter_id is None else "run_filter",
                db.jsonb(payload),
                dedupe_key,
            ),
        )
    if row:
        events.publish_task(row["id"])
    return RunAdmission(row["id"] if row else None)
