"""Operator recovery of a failed task without discarding accepted provider work."""

from __future__ import annotations

from typing import Any

from api import db, events
from api.ai import batch_results, request_snapshots
from core.payload_objects import PayloadStore, PayloadUnavailable
from core.pool import in_transaction

MARKER = "payload_recovery"


def retry(task_id: int, store: PayloadStore | None = None) -> str:
    if in_transaction():
        raise RuntimeError("Payload recovery cannot run inside a database transaction")
    # Import lazily: runtime lifecycle itself records this marker on failure.
    from api import task_jobs
    from tasks.runtime.lifecycle import CHUNK_KINDS

    source = db.query_one("SELECT * FROM tasks WHERE id=%s", (task_id,))
    if not source or source["status"] != "failed":
        return "conflict"
    payload = source["payload"] or {}
    if payload.get(MARKER) != {"reason": "payload_unavailable"}:
        return "conflict"
    parent_id = source["parent_id"] or payload.get("parent_id")
    parent = None
    if parent_id is not None:
        if source["kind"] not in CHUNK_KINDS:
            return "conflict"
        parent = db.query_one("SELECT * FROM tasks WHERE id=%s", (parent_id,))
        if not parent or not _recoverable_parent(parent):
            return "conflict"
    requests = db.query(
        "SELECT * FROM batch_requests WHERE task_id=%s ORDER BY custom_id", (task_id,)
    )
    receipts = db.query(
        "SELECT * FROM batch_result_receipts WHERE task_id=%s ORDER BY provider_batch_id,custom_id",
        (task_id,),
    )
    unconsumed = [row for row in receipts if row["consumed_at"] is None]
    required_ids = {row["custom_id"] for row in unconsumed}
    collection_finished = payload.get("batch_collection_checkpointed") is True and not payload.get(
        "batch_ids"
    )
    try:
        if source["kind"] in (*task_jobs.MANAGED_BOARD_RUNS.kinds, *task_jobs.FILTER_CHUNKS.kinds):
            task_jobs.run_jobs(payload, store)
        for row in requests:
            if not collection_finished or row["custom_id"] in required_ids:
                request_snapshots.resolve(row, store)
        for row in unconsumed:
            batch_results.response_payload(row["response"], store)
    except PayloadUnavailable:
        return "unavailable"
    with db.transaction():
        db.execute("SET LOCAL lock_timeout='2s'")
        db.execute("SET LOCAL statement_timeout='5s'")
        # Parent finalization uses this same lock before counting live children.
        if (
            parent is not None
            and db.query_one("SELECT * FROM tasks WHERE id=%s FOR UPDATE", (parent_id,)) != parent
        ):
            return "conflict"
        if db.query_one("SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,)) != source:
            return "conflict"
        current_requests = db.query(
            "SELECT * FROM batch_requests WHERE task_id=%s ORDER BY custom_id FOR UPDATE",
            (task_id,),
        )
        current_receipts = db.query(
            "SELECT * FROM batch_result_receipts WHERE task_id=%s "
            "ORDER BY provider_batch_id,custom_id FOR UPDATE",
            (task_id,),
        )
        if current_requests != requests or current_receipts != receipts:
            return "conflict"
        # A provider ledger without preserved replay state is ambiguous. Never
        # convert that ambiguity into a fresh paid submission.
        accepted = db.query_one("SELECT 1 FROM ai_batches WHERE task_id=%s LIMIT 1", (task_id,))
        replayable = (
            payload.get("batch_ids")
            or payload.get("batch_collection_checkpointed") is True
            or batch_results.has_results(task_id)
        )
        if accepted and not replayable:
            return "conflict"
        if parent is not None and parent["status"] == "failed":
            db.execute(
                "UPDATE tasks SET status='waiting',error=NULL,finished_at=NULL WHERE id=%s",
                (parent_id,),
            )
        db.execute(
            "UPDATE tasks SET status='pending',payload=payload-%s,error=NULL,finished_at=NULL,"
            "started_at=NULL,last_heartbeat=NULL,worker=NULL WHERE id=%s",
            (MARKER, task_id),
        )
    events.publish_task(task_id)
    if parent is not None:
        events.publish_task(parent_id)
    return "pending"


def _recoverable_parent(parent: dict[str, Any]) -> bool:
    import re

    return parent["status"] == "waiting" or (
        parent["status"] == "failed"
        and re.fullmatch(r"[1-9][0-9]* chunk\(s\) failed", parent["error"] or "") is not None
    )
