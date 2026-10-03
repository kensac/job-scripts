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
) -> ReceiptProgress:
    if not backup_complete:
        raise ValueError("Compaction requires a completed independent database backup")
    if in_transaction():
        raise RuntimeError("Payload compaction cannot run inside a database transaction")
    if min(limit, group_size, workers, byte_budget) <= 0 or workers > group_size:
        raise ValueError("Positive limits and workers no greater than group size are required")
    result = ReceiptProgress(after)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        remaining = limit
        while remaining:
            try:
                sources, blocked = read_group(
                    after=result.after,
                    limit=min(remaining, group_size),
                    byte_budget=byte_budget,
                    inline_only=True,
                )
            except Error:
                result.counts["unavailable"] += 1
                result.stop_reason = "database_error"
                return result
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
                for source, (_, size) in zip(
                    sources[:committed], prepared[:committed], strict=True
                ):
                    result.counts["compacted"] += 1
                    result.logical_bytes_verified += size
                    result.after = source.cursor
                    remaining -= 1
                if changed or stop < len(sources):
                    outcome = "changed" if changed else prepared[stop][0]
                    result.counts[outcome] += 1
                    result.stop_reason = outcome
                    return result
            if blocked is not None:
                result.counts["unavailable"] += 1
                result.stop_reason = blocked
                return result
            if not sources:
                break
    return result
