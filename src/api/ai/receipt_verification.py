"""Read-only verification with bounded groups and ordered progress."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from api import db
from api.ai.receipt_payload_groups import Cursor, ReceiptProgress, Source, read_group, verify_object
from api.ai.receipt_payloads import _ELIGIBLE
from core.payload_objects import PayloadStore
from core.pool import in_transaction


def _current(sources: list[Source]) -> dict[Cursor, bool]:
    # Compare the exact database serialization, not a reconstructed JSON value
    # or a digest alone. Return booleans so changed oversized rows are never
    # hydrated into client memory during the recheck.
    rows = db.query(
        "SELECT wanted.batch_id,wanted.custom_id,COALESCE(("
        f"{_ELIGIBLE} AND to_jsonb(r)::text=wanted.snapshot),false) AS current "
        "FROM unnest(%s::text[],%s::text[],%s::text[]) "
        "AS wanted(batch_id,custom_id,snapshot) LEFT JOIN batch_result_receipts r "
        "ON r.provider_batch_id=wanted.batch_id AND r.custom_id=wanted.custom_id "
        "LEFT JOIN tasks t ON t.id=r.task_id",
        (
            [source.cursor[0] for source in sources],
            [source.cursor[1] for source in sources],
            [source.snapshot for source in sources],
        ),
    )
    return {(row["batch_id"], row["custom_id"]): row["current"] for row in rows}


def verify(
    store: PayloadStore,
    *,
    after: Cursor | None,
    limit: int,
    group_size: int,
    workers: int,
    byte_budget: int,
) -> ReceiptProgress:
    if in_transaction():
        raise RuntimeError("Payload verification cannot run inside a database transaction")
    if min(limit, group_size, workers, byte_budget) <= 0 or workers > group_size:
        raise ValueError("Positive limits and workers no greater than group size are required")
    result = ReceiptProgress(after)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        remaining = limit
        while remaining:
            sources, blocked = read_group(
                after=result.after, limit=min(remaining, group_size), byte_budget=byte_budget
            )
            if sources:
                # Only this byte-bounded group is submitted. Futures retain
                # scalar outcomes, never decoded vectors or source documents.
                outcomes = list(executor.map(lambda source: verify_object(source, store), sources))
                current = _current(sources)
                for source, (object_outcome, size) in zip(sources, outcomes, strict=True):
                    outcome = object_outcome if current.get(source.cursor, False) else "changed"
                    result.counts[outcome] += 1
                    if outcome != "verified":
                        result.stop_reason = outcome
                        return result
                    result.logical_bytes_verified += size
                    result.after = source.cursor
                    remaining -= 1
            if blocked is not None:
                result.counts["unavailable"] += 1
                result.stop_reason = blocked
                break
            if not sources:
                break
    return result
