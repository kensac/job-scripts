"""Rewrites receipt vectors still held as version 1 gzip objects as version 2.

Version 2 is the only format written since the reference existed; 501 of
1,619 vector receipts still named a version 1 object on 2026-10-10. Once none
do, the gzip reader in core.payload_objects is removed.

Idempotent by predicate (the reference's version is 1): a second run finds
nothing, and a run cut short resumes where the rows are. Each object is read
and verified through the existing reader, written again with put_verified
outside any transaction, and only then swapped in, by a conditional update
that requires the row to still hold the reference that was read. The old
object stays where it is; objects are never deleted.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from api import db
from core.payload_objects import MAX_CONNECTIONS, PayloadRef, PayloadStore, PayloadUnavailable
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

KIND = "rewrite_receipt_vectors_v1"
LABEL = "rewriting version 1 receipt vectors"
# One object read and one write per row; a batch is one round of the client's
# connection pool, so a cancelled run loses at most that many uploads.
BATCH = MAX_CONNECTIONS

_PENDING = "response->'embedding_vectors_ref'->>'version' = '1'"


def remaining() -> int:
    row = db.query_one(f"SELECT count(*) AS n FROM batch_result_receipts WHERE {_PENDING}")
    return int(row["n"]) if row else 0


def _rewrite(store: PayloadStore, reference: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return dataclasses.asdict(store.put_verified(store.get(PayloadRef.parse(reference))))
    except PayloadUnavailable:
        return None


def rewrite_batch(store: PayloadStore, after: tuple[str, str]) -> tuple[int, int, tuple[str, str]]:
    """(rewritten, unavailable, cursor) for the next BATCH rows after `after`.

    An unavailable object is counted and passed over, so one bad object cannot
    hold the rest; the next run tries it again."""
    rows = db.query(
        "SELECT provider_batch_id, custom_id, response->'embedding_vectors_ref' AS ref "
        f"FROM batch_result_receipts WHERE {_PENDING} AND (provider_batch_id, custom_id) > (%s, %s) "
        "ORDER BY provider_batch_id, custom_id LIMIT %s",
        (*after, BATCH),
    )
    if not rows:
        return 0, 0, after
    with ThreadPoolExecutor(max_workers=MAX_CONNECTIONS) as executor:
        new = list(executor.map(lambda row: _rewrite(store, row["ref"]), rows))
    rewritten = 0
    with db.transaction():
        for row, ref in zip(rows, new, strict=True):
            if ref is None:
                continue
            rewritten += db.execute_count(
                "UPDATE batch_result_receipts "
                "SET response = jsonb_set(response, '{embedding_vectors_ref}', %s) "
                "WHERE provider_batch_id = %s AND custom_id = %s "
                "AND response->'embedding_vectors_ref' = %s",
                (db.jsonb(ref), row["provider_batch_id"], row["custom_id"], db.jsonb(row["ref"])),
            )
    last = rows[-1]
    return rewritten, new.count(None), (last["provider_batch_id"], last["custom_id"])


async def handle_rewrite_receipt_vectors_v1(task_id: int, payload: dict[str, Any]) -> None:
    total = await asyncio.to_thread(remaining)
    done = unavailable = 0
    set_progress(task_id, 0, total, LABEL)
    if total:
        store = PayloadStore.from_env()
        cursor = ("", "")
        while not cancelled(task_id):
            n, missing, after = await asyncio.to_thread(rewrite_batch, store, cursor)
            if after == cursor:
                break
            done, unavailable, cursor = done + n, unavailable + missing, after
            set_progress(task_id, done, total, LABEL)
    extra = {"rewritten": done, "unavailable": unavailable}
    set_progress(task_id, done, total, f"rewrote {done} of {total} receipts", extra)
    logger.info("%s: %s of %s, %s unavailable", KIND, done, total, unavailable)
