"""Bounded, reversible archival of consumed embedding vectors."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from api import db
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable
from core.pool import in_transaction

Mode = Literal["copy", "compact", "restore", "verify"]
REFERENCE = "embedding_vectors_ref"
_ELIGIBLE = (
    "t.kind='embed_postings_batch' AND t.status='done' "
    "AND r.consumed_at IS NOT NULL AND r.outcome IS NOT NULL"
)


def candidates(*, after: tuple[str, str] | None, limit: int, mode: Mode) -> list[dict[str, Any]]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    predicate = {
        "copy": "jsonb_typeof(r.response->'embedding_vectors')='array'",
        "compact": "r.response ? 'embedding_vectors' AND r.response ? 'embedding_vectors_ref'",
        "restore": "r.response ? 'embedding_vectors_ref'",
        "verify": "r.response ? 'embedding_vectors_ref'",
    }[mode]
    return db.query(
        "SELECT r.* FROM batch_result_receipts r JOIN tasks t ON t.id=r.task_id "
        f"WHERE {_ELIGIBLE} AND {predicate} "
        "AND (%s::text IS NULL OR (r.provider_batch_id,r.custom_id)>(%s,%s)) "
        "ORDER BY r.provider_batch_id,r.custom_id LIMIT %s",
        (after[0] if after else None, *(after or (None, None)), limit),
    )


def _current(source: dict[str, Any], *, lock: bool = False) -> bool:
    # Lock the parent first, as the worker does when changing task state.
    # A predicate-only UPDATE of the receipt cannot prevent a concurrent
    # task reactivation after its statement snapshot was taken.
    task = db.query_one(
        "SELECT kind,status FROM tasks WHERE id=%s" + (" FOR UPDATE" if lock else ""),
        (source["task_id"],),
    )
    if task != {"kind": "embed_postings_batch", "status": "done"}:
        return False
    row = db.query_one(
        "SELECT * FROM batch_result_receipts WHERE provider_batch_id=%s AND custom_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (source["provider_batch_id"], source["custom_id"]),
    )
    return (
        row == source
        and row is not None
        and row["consumed_at"] is not None
        and row["outcome"] is not None
    )


def migrate(source: dict[str, Any], store: PayloadStore, *, mode: Mode) -> str:
    if in_transaction():
        raise RuntimeError("Payload migration cannot run inside a database transaction")
    if mode not in ("copy", "compact", "restore", "verify"):
        raise ValueError("unsupported migration mode")
    if not _current(source):
        return "changed"
    response = source["response"]
    updated = dict(response)
    ref = PayloadRef.parse(response[REFERENCE]) if REFERENCE in response else None
    inline = response.get("embedding_vectors")
    if mode == "copy" and ref is None:
        if not isinstance(inline, list):
            return "ineligible"
        ref = store.put_verified(inline)
        updated[REFERENCE] = asdict(ref)
        outcome = "copied"
    else:
        if ref is None:
            raise PayloadUnavailable("Receipt has no verified vector reference; copy first")
        vectors = store.get(ref)
        if not isinstance(vectors, list):
            raise PayloadUnavailable("Receipt vector object is not an array")
        if inline is not None and vectors != inline:
            raise PayloadUnavailable("Receipt vectors differ from the object")
        outcome = "verified"
        if mode == "compact":
            updated.pop("embedding_vectors", None)
            outcome = "compacted" if updated != response else "verified"
        elif mode == "restore":
            updated["embedding_vectors"] = vectors
            updated.pop(REFERENCE)
            outcome = "restored"
    if mode == "verify":
        return "verified" if _current(source) else "changed"
    # All object IO has finished. The transaction is only the eligibility
    # recheck and a conditional replacement of the exact source response.
    with db.transaction():
        db.execute("SET LOCAL lock_timeout='2s'")
        db.execute("SET LOCAL statement_timeout='5s'")
        if not _current(source, lock=True):
            return "changed"
        if updated != response:
            changed = db.execute_count(
                "UPDATE batch_result_receipts SET response=%s "
                "WHERE provider_batch_id=%s AND custom_id=%s AND task_id=%s AND response=%s "
                "AND consumed_at IS NOT NULL AND outcome IS NOT NULL",
                (
                    db.jsonb(updated),
                    source["provider_batch_id"],
                    source["custom_id"],
                    source["task_id"],
                    db.jsonb(response),
                ),
            )
            if changed != 1:
                return "changed"
    return outcome
