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
    scan_limit: int | None = None,
) -> ReceiptProgress:
    if in_transaction():
        raise RuntimeError("Payload verification cannot run inside a database transaction")
    if min(limit, group_size, workers, byte_budget) <= 0 or workers > group_size:
        raise ValueError("Positive limits and workers no greater than group size are required")
    scan_remaining = limit * group_size if scan_limit is None else scan_limit
    if scan_remaining <= 0:
        raise ValueError("Scan limit must be positive")
    result = ReceiptProgress(after)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        remaining = limit
        while remaining and scan_remaining:
            page = read_group(
                after=result.after,
                limit=min(remaining, group_size, scan_remaining),
                byte_budget=byte_budget,
            )
            result.scanned += page.scanned
            scan_remaining -= page.scanned
            sources = page.sources
            outcomes = list(executor.map(lambda source: verify_object(source, store), sources))
            current = _current(sources) if sources else {}
            prepared = dict(zip((source.cursor for source in sources), outcomes, strict=True))
            for cursor in page.cursors:
                if cursor not in prepared:
                    result.counts["skipped"] += 1
                    result.after = cursor
                    continue
                object_outcome, size = prepared[cursor]
                outcome = object_outcome if current.get(cursor, False) else "changed"
                result.counts[outcome] += 1
                if outcome != "verified":
                    result.stop_reason = outcome
                    return result
                result.logical_bytes_verified += size
                result.after = result.verified_after = cursor
                remaining -= 1
            if page.blocked is not None:
                result.counts["unavailable"] += 1
                result.stop_reason = page.blocked
                break
            if page.exhausted:
                result.exhausted = True
                break
    return result
