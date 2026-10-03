"""Bounded storage operations for completed non-profile request evidence."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from api import db
from api.ai import request_snapshots
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable
from core.pool import in_transaction

Mode = Literal["copy", "compact", "restore", "verify"]
_ELIGIBLE = (
    "t.status='done' AND t.kind<>'classify_job_profiles' "
    "AND NOT EXISTS(SELECT 1 FROM batch_result_receipts r "
    "WHERE r.task_id=t.id AND r.consumed_at IS NULL)"
)


def candidates(*, after: tuple[int, str] | None, limit: int, mode: Mode) -> list[dict[str, Any]]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    predicate = {
        "copy": "b.snapshot IS NOT NULL",
        "compact": "b.snapshot IS NOT NULL AND b.snapshot_ref IS NOT NULL",
        "restore": "b.snapshot_ref IS NOT NULL",
        "verify": "b.snapshot_ref IS NOT NULL",
    }[mode]
    return db.query(
        "SELECT b.* FROM batch_requests b JOIN tasks t ON t.id=b.task_id "
        f"WHERE {_ELIGIBLE} AND {predicate} "
        "AND (%s::bigint IS NULL OR (b.task_id,b.custom_id)>(%s,%s)) "
        "ORDER BY b.task_id,b.custom_id LIMIT %s",
        (after[0] if after else None, *(after or (None, None)), limit),
    )


def _current(source: dict[str, Any], *, lock: bool = False) -> bool:
    task = db.query_one(
        "SELECT kind,status FROM tasks WHERE id=%s" + (" FOR UPDATE" if lock else ""),
        (source["task_id"],),
    )
    if not task or task["status"] != "done" or task["kind"] == "classify_job_profiles":
        return False
    row = db.query_one(
        "SELECT * FROM batch_requests WHERE task_id=%s AND custom_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (source["task_id"], source["custom_id"]),
    )
    return row == source and not db.query_one(
        "SELECT 1 FROM batch_result_receipts WHERE task_id=%s AND consumed_at IS NULL LIMIT 1",
        (source["task_id"],),
    )


def migrate(source: dict[str, Any], store: PayloadStore, *, mode: Mode) -> str:
    if in_transaction():
        raise RuntimeError("Snapshot migration cannot run inside a database transaction")
    if mode not in ("copy", "compact", "restore", "verify"):
        raise ValueError("unsupported migration mode")
    if not _current(source):
        return "changed"
    inline, reference = source["snapshot"], source["snapshot_ref"]
    updated, updated_ref = inline, reference
    if mode == "copy" and reference is None:
        if inline is None:
            return "ineligible"
        request_snapshots.resolve(source, store)
        updated_ref = asdict(store.put_verified(inline))
        outcome = "copied"
    else:
        if reference is None:
            raise PayloadUnavailable("Snapshot has no verified reference; copy first")
        restored = store.get(PayloadRef.parse(reference))
        request_snapshots.resolve({**source, "snapshot": restored}, store)
        if inline is not None and inline != restored:
            raise PayloadUnavailable("Snapshot differs from its object")
        outcome = "verified"
        if mode == "compact":
            updated = None
            outcome = "compacted" if inline is not None else "verified"
        elif mode == "restore":
            updated, updated_ref = restored, None
            outcome = "restored"
    if mode == "verify":
        return "verified" if _current(source) else "changed"
    with db.transaction():
        db.execute("SET LOCAL lock_timeout='2s'")
        db.execute("SET LOCAL statement_timeout='5s'")
        if not _current(source, lock=True):
            return "changed"
        if (updated, updated_ref) != (inline, reference):
            changed = db.execute_count(
                "UPDATE batch_requests SET snapshot=%s,snapshot_ref=%s "
                "WHERE task_id=%s AND custom_id=%s "
                "AND snapshot IS NOT DISTINCT FROM %s AND snapshot_ref IS NOT DISTINCT FROM %s",
                (
                    db.jsonb(updated) if updated is not None else None,
                    db.jsonb(updated_ref) if updated_ref is not None else None,
                    source["task_id"],
                    source["custom_id"],
                    db.jsonb(inline) if inline is not None else None,
                    db.jsonb(reference) if reference is not None else None,
                ),
            )
            if changed != 1:
                return "changed"
    return outcome
