from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from api import db, events


class InFlight(BaseModel):
    """The task a person is waiting on, as the page shows it.

    Declared here rather than in the router that surfaces it, because the
    columns are this function's and a router copying them is how the two
    drift. `filter_runs` finds its own conflicting task with its own query
    and returns this same shape, which is why `kind` is here: it was already
    in that query, and one model for one idea beats two that agree today."""

    id: int
    kind: str
    status: str
    progress: dict[str, Any] | None = None
    created_at: datetime.datetime


ACTIVE_STATUSES = ("pending", "running", "awaiting_batch", "waiting")
TASK_STATUSES = ("pending", "waiting", "running", "awaiting_batch", "done", "failed", "cancelled")
TaskKind = Literal["application_draft", "extract_upload", "ingest_source"]


def in_flight(
    kind: TaskKind, subject: dict[str, Any], *, statuses: tuple[str, ...] = ACTIVE_STATUSES
) -> InFlight | None:
    clauses = ["kind = %(kind)s", "status = ANY(%(statuses)s)"]
    params: dict[str, Any] = {"kind": kind, "statuses": list(statuses)}
    for index, (key, value) in enumerate(subject.items()):
        expression = f"payload->>%(key{index})s"
        if isinstance(value, int):
            expression = f"({expression})::bigint"
        clauses.append(f"{expression} = %(value{index})s")
        params[f"key{index}"] = key
        params[f"value{index}"] = value
    return db.query_one_as(
        InFlight,
        "SELECT id, kind, status, progress, created_at FROM tasks WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT 1",
        params,
    )


@dataclass(frozen=True)
class Admission:
    task_id: int | None = None
    conflict: InFlight | None = None


def enqueue(
    kind: TaskKind,
    subject: dict[str, Any],
    payload: dict[str, Any],
    *,
    scheduled: bool = False,
    dedupe_key: str | None = None,
) -> Admission:
    if scheduled and kind != "ingest_source":
        raise ValueError("scheduled admission requires an ingest source")
    with db.transaction():
        # Lock a stable domain row, including when no task exists yet. Upload
        # already locks jobs while upserting, so every extraction writer uses
        # that same lock rather than introducing a second lock order.
        if kind == "ingest_source":
            owner = db.query_one(
                "SELECT active, ingest_interval_hours FROM sources WHERE name = %s FOR UPDATE",
                (subject["source"],),
            )
        else:
            owner = db.query_one(
                "SELECT id, extraction_status FROM jobs WHERE id = %s FOR UPDATE",
                (subject["job_id"],),
            )
        if owner is None:
            raise LookupError("task subject no longer exists")
        statuses = ("pending",) if scheduled else ACTIVE_STATUSES
        conflict = in_flight(kind, subject, statuses=statuses)
        if conflict:
            return Admission(conflict=conflict)
        if scheduled:
            if not owner["active"]:
                return Admission()
            # Scheduled hourly pulls may queue behind a running pull. Longer
            # source intervals also respect recent successful/in-flight work.
            if owner["ingest_interval_hours"] > 1 and db.query_one(
                "SELECT 1 FROM tasks WHERE kind = 'ingest_source' "
                "AND payload->>'source' = %s AND status IN ('running', 'done') "
                "AND created_at > now() - make_interval(hours => %s)",
                (subject["source"], owner["ingest_interval_hours"]),
            ):
                return Admission()
        if kind == "extract_upload":
            if payload.get("force"):
                db.execute(
                    "UPDATE jobs SET extraction_status = 'pending' WHERE id = %s",
                    (subject["job_id"],),
                )
            elif owner["extraction_status"] != "pending":
                return Admission()
        row = db.query_one(
            "INSERT INTO tasks (kind, payload, dedupe_key) VALUES (%s, %s, %s) "
            "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
            (kind, db.jsonb({**payload, **subject}), dedupe_key),
        )
        if row and kind == "application_draft":
            from api.apply.writes import reserve_task

            reserve_task(row["id"], subject["user_id"])
    if row:
        events.publish_task(row["id"])
    return Admission(task_id=row["id"] if row else None)
