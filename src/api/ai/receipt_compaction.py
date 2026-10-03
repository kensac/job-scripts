"""Verify outside locks, then compact only an unchanged ordered prefix."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from psycopg import Error

from api import db
from api.ai.receipt_payload_groups import Cursor, ReceiptProgress, Source, read_group, verify_object
from api.ai.receipt_payloads import _ELIGIBLE
from core.payload_objects import PayloadStore
from core.pool import in_transaction


class CompactionSourceChanged(RuntimeError):
    pass


def _commit(sources: list[Source]) -> tuple[int, bool]:
    if not sources:
        return 0, False
    with db.transaction():
        db.execute("SET LOCAL lock_timeout='2s'")
        db.execute("SET LOCAL statement_timeout='5s'")
        # Parent-first ordering matches the worker. Only original parents need
        # locking: moving a receipt makes its exact source comparison fail.
        db.query(
            "SELECT id FROM tasks WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
            (sorted({source.task_id for source in sources}),),
        )
        current = {
            (row["provider_batch_id"], row["custom_id"]): row["current"]
            for row in db.query(
                "SELECT r.provider_batch_id,r.custom_id,COALESCE(("
                f"{_ELIGIBLE} AND to_jsonb(r)::text=wanted.snapshot),false) AS current "
                "FROM batch_result_receipts r JOIN unnest(%s::text[],%s::text[],%s::text[]) "
                "AS wanted(batch_id,custom_id,snapshot) ON r.provider_batch_id=wanted.batch_id "
                "AND r.custom_id=wanted.custom_id JOIN tasks t ON t.id=r.task_id "
                "ORDER BY r.provider_batch_id,r.custom_id FOR UPDATE OF r",
                (
                    [source.cursor[0] for source in sources],
                    [source.cursor[1] for source in sources],
                    [source.snapshot for source in sources],
                ),
            )
        }
        stop = next(
            (i for i, source in enumerate(sources) if not current.get(source.cursor, False)),
            len(sources),
        )
        prefix = sources[:stop]
        if prefix:
            changed = db.execute_count(
                "UPDATE batch_result_receipts r SET response=r.response-'embedding_vectors' "
                "FROM unnest(%s::text[],%s::text[],%s::text[]) "
                "AS wanted(batch_id,custom_id,snapshot),tasks t "
                "WHERE r.provider_batch_id=wanted.batch_id AND r.custom_id=wanted.custom_id "
                "AND t.id=r.task_id AND to_jsonb(r)::text=wanted.snapshot "
                f"AND {_ELIGIBLE} AND r.response ? 'embedding_vectors'",
                (
                    [source.cursor[0] for source in prefix],
                    [source.cursor[1] for source in prefix],
                    [source.snapshot for source in prefix],
                ),
            )
            if changed != len(prefix):
                raise CompactionSourceChanged("Receipt sources changed while locked")
        return len(prefix), stop < len(sources)


def compact(
    store: PayloadStore,
    *,
    after: Cursor | None,
    limit: int,
    group_size: int,
    workers: int,
    byte_budget: int,
    backup_complete: bool = False,
    scan_limit: int | None = None,
) -> ReceiptProgress:
    if not backup_complete:
        raise ValueError("Compaction requires a completed independent database backup")
    if in_transaction():
        raise RuntimeError("Payload compaction cannot run inside a database transaction")
    if min(limit, group_size, workers, byte_budget) <= 0 or workers > group_size:
        raise ValueError("Positive limits and workers no greater than group size are required")
    scan_remaining = limit * group_size if scan_limit is None else scan_limit
    if scan_remaining <= 0:
        raise ValueError("Scan limit must be positive")
    result = ReceiptProgress(after)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        remaining = limit
        while remaining and scan_remaining:
            try:
                page = read_group(
                    after=result.after,
                    limit=min(remaining, group_size, scan_remaining),
                    byte_budget=byte_budget,
                    inline_only=True,
                )
            except Error:
                result.counts["unavailable"] += 1
                result.stop_reason = "database_error"
                return result
            result.scanned += page.scanned
            scan_remaining -= page.scanned
            sources = page.sources
            committed = 0
            changed = False
            prepared = []
            stop = 0
            if sources:
                prepared = list(executor.map(lambda source: verify_object(source, store), sources))
                stop = next(
                    (i for i, (outcome, _) in enumerate(prepared) if outcome != "verified"),
                    len(prepared),
                )
                try:
                    committed, changed = _commit(sources[:stop])
                except CompactionSourceChanged:
                    result.counts["changed"] += 1
                    result.stop_reason = "changed"
                    return result
                except Error:
                    result.counts["unavailable"] += 1
                    result.stop_reason = "database_error"
                    return result
            committed_sizes = {
                source.cursor: outcome[1]
                for source, outcome in zip(sources[:committed], prepared[:committed], strict=True)
            }
            candidate_keys = {source.cursor for source in sources}
            for cursor in page.cursors:
                if cursor in committed_sizes:
                    result.counts["compacted"] += 1
                    result.logical_bytes_verified += committed_sizes[cursor]
                    result.after = result.verified_after = cursor
                    remaining -= 1
                elif cursor not in candidate_keys:
                    result.counts["skipped"] += 1
                    result.after = cursor
                else:
                    outcome = "changed" if changed else prepared[stop][0]
                    result.counts[outcome] += 1
                    result.stop_reason = outcome
                    return result
            if page.blocked is not None:
                result.counts["unavailable"] += 1
                result.stop_reason = page.blocked
                return result
            if page.exhausted:
                result.exhausted = True
                break
    return result
