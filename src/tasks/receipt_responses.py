"""Move receipt responses still inline into object storage.

api.ai.batch_results.checkpoint uploads a receipt's response before writing
the row, so a response is inline only on rows written before that, or while
an older image ran (api.data_level). A run moves such rows, oldest first,
until none is left; the schedule starts one only when none is pending or
running, and moving waits until every running worker is at data level 3,
since an older image reads the response column itself. Switching
`receipt_response_move_enabled` off stops a run between steps. Nothing is deleted:
a moved row keeps its key, outcome and consumption.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import data_level, db
from api.ai.batch_results import output_tokens_of, response_digest
from core.payloads.objects import (
    BUNDLE_MAX_BYTES,
    MAX_CONNECTIONS,
    PayloadStore,
    encode_payload,
    put_values,
    shared_store,
)
from core.pool import connection
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

KIND = "move_receipt_responses"

# The rows a move reads, served by idx_batch_result_receipts_inline, whose
# predicate is this condition spelled the same way.
INLINE = "response IS NOT NULL"

# Responses per step: a full bundle for every upload connection, so a step
# keeps the whole pool busy, and a step holds no more than that however short
# or long the responses it meets.
_STEP_BYTES = MAX_CONNECTIONS * BUNDLE_MAX_BYTES


def _step() -> list[dict[str, Any]]:
    """The oldest inline receipts, read through a cursor until their
    responses hold _STEP_BYTES."""
    rows: list[dict[str, Any]] = []
    size = 0
    with (
        connection() as conn,
        conn.transaction(),
        conn.cursor(name="inline_receipt_responses") as cur,
    ):
        cur.itersize = MAX_CONNECTIONS
        cur.execute(
            "SELECT provider_batch_id, custom_id, response FROM batch_result_receipts "
            f"WHERE {INLINE} ORDER BY received_at"
        )
        for row in cur:
            rows.append(row)
            size += len(encode_payload(row["response"]))
            if size >= _STEP_BYTES:
                break
    return rows


def move(store: PayloadStore) -> int:
    """Move one step of inline responses, returning how many. Uploaded and
    read back before any row changes (PayloadStore.put_bundle), and the
    update only touches rows still inline. A response is never rewritten
    once received."""
    rows = _step()
    if not rows:
        return 0
    digests = [response_digest(row["response"]) for row in rows]
    links = put_values(
        {digest: row["response"] for digest, row in zip(digests, rows, strict=True)}, store
    )
    return db.execute_count(
        """
        UPDATE batch_result_receipts r SET response = NULL, response_sha256 = m.member,
               response_object = m.object, response_object_size = m.size,
               finish_reason = r.response->>'finish_reason', output_tokens = m.output_tokens
        FROM unnest(%(batches)s::text[], %(customs)s::text[], %(members)s::bytea[],
                    %(objects)s::bytea[], %(sizes)s::int[], %(tokens)s::int[])
             AS m(provider_batch_id, custom_id, member, object, size, output_tokens)
        WHERE r.provider_batch_id = m.provider_batch_id AND r.custom_id = m.custom_id
          AND r.response IS NOT NULL
        """,
        {
            "batches": [row["provider_batch_id"] for row in rows],
            "customs": [row["custom_id"] for row in rows],
            "members": [bytes.fromhex(digest) for digest in digests],
            "objects": [links[digest][0] for digest in digests],
            "sizes": [links[digest][1] for digest in digests],
            "tokens": [output_tokens_of(row["response"]) for row in rows],
        },
    )


def inline_count() -> int:
    """Receipts whose response is still inline: an index-only count of
    idx_batch_result_receipts_inline."""
    row = db.query_one(f"SELECT count(*) AS n FROM batch_result_receipts WHERE {INLINE}")
    return row["n"] if row else 0


async def handle_move_receipt_responses(task_id: int, payload: dict[str, Any]) -> None:
    behind = await asyncio.to_thread(data_level.behind, data_level.LEVEL)
    if behind:
        set_progress(task_id, 0, 0, f"waiting for {behind} older workers", extra={"behind": behind})
        return
    store = shared_store()
    total = await asyncio.to_thread(inline_count)
    moved = 0
    while db.get_config("receipt_response_move_enabled") and not cancelled(task_id):
        n = await asyncio.to_thread(move, store)
        if not n:
            break
        moved += n
        total = max(total, moved)
        set_progress(task_id, moved, total, f"moved {moved} receipt responses to object storage")
    set_progress(
        task_id,
        moved,
        moved,
        f"moved {moved} receipt responses to object storage",
        extra={"moved": moved},
    )
    logger.info("%s: moved %s", KIND, moved)
