from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from api import db, events

ACTIVE_STATUSES = ("pending", "running", "awaiting_batch", "waiting")
TASK_STATUSES = ("pending", "waiting", "running", "awaiting_batch", "done", "failed", "cancelled")
TaskKind = Literal["application_draft", "extract_upload", "ingest_source"]


def in_flight(
    kind: TaskKind, subject: dict[str, Any], *, statuses: tuple[str, ...] = ACTIVE_STATUSES
) -> dict | None:
    clauses = ["kind = %(kind)s", "status = ANY(%(statuses)s)"]
    params: dict[str, Any] = {"kind": kind, "statuses": list(statuses)}
    for index, (key, value) in enumerate(subject.items()):
        expression = f"payload->>%(key{index})s"
        if isinstance(value, int):
            expression = f"({expression})::bigint"
        clauses.append(f"{expression} = %(value{index})s")
        params[f"key{index}"] = key
        params[f"value{index}"] = value
    return db.query_one(
        "SELECT id, status, progress, created_at FROM tasks WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT 1",
        params,
    )


@dataclass(frozen=True)
class Admission:
    task_id: int | None = None
    conflict: dict | None = None


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
            from api.application_writes import reserve_task

            reserve_task(row["id"], subject["user_id"])
    if row:
        events.publish_task(row["id"])
    return Admission(task_id=row["id"] if row else None)
