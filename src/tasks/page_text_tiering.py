"""Move page text still inline into object storage.

core.page_fetches.record uploads a fetch's text before writing the row, so
text is inline only on rows written before that, or while storage was
unavailable (docs/agents/sources-and-boards.md, "A page's text is held by
reference"). A run moves such rows, oldest first, until none is left; the
schedule starts one only when none is pending or running. Nothing is
deleted; a moved row keeps its id, so every content_row_id and page_fetch_id
that names it stays true. Moving waits until every running worker is at
api.data_level 2, and stops between steps when `page_text_move_enabled` is
switched off. The task keeps its kind, which the worker's schedule and
its history key on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import data_level, db
from core import page_fetches
from core.payloads.objects import MAX_CONNECTIONS, shared_store
from core.pool import connection
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

# The rows a move reads, served by idx_page_fetches_inline_text, whose
# predicate is this condition spelled the same way.
INLINE = "content <> ''"

# Text per step: a full page bundle for every upload connection, so a step
# keeps the whole pool busy, and a step holds no more text than that however
# short or long the rows it meets.
_STEP_BYTES = MAX_CONNECTIONS * page_fetches.PAGE_BUNDLE_MAX_BYTES


def _step() -> list[dict[str, Any]]:
    """The oldest inline rows, read through a cursor until they hold
    _STEP_BYTES of text."""
    rows: list[dict[str, Any]] = []
    size = 0
    with connection() as conn, conn.transaction(), conn.cursor(name="inline_page_text") as cur:
        cur.itersize = MAX_CONNECTIONS
        cur.execute(f"SELECT id, content FROM page_fetches WHERE {INLINE} ORDER BY id")
        for row in cur:
            rows.append(row)
            size += len(row["content"])
            if size >= _STEP_BYTES:
                break
    return rows


def move() -> int:
    """Move one step of inline rows, returning how many. Uploaded and read
    back before any row is cleared (PayloadStore.put_bundle), and the clear
    only touches rows still inline."""
    rows = _step()
    if not rows:
        return 0
    # The shared store with its retries: a move has no fetch waiting on it.
    links = page_fetches.upload((r["content"] for r in rows), shared_store())
    digests = {r["id"]: page_fetches.digest(r["content"]) for r in rows}
    return db.execute_count(
        """
        UPDATE page_fetches f SET content = NULL, content_sha256 = m.member,
               content_object = m.object, content_object_size = m.size, content_chars = m.chars
        FROM unnest(%(ids)s::bigint[], %(members)s::bytea[], %(objects)s::bytea[],
                    %(sizes)s::int[], %(chars)s::int[]) AS m(id, member, object, size, chars)
        WHERE f.id = m.id AND f.content IS NOT NULL
        """,
        {
            "ids": [r["id"] for r in rows],
            "members": [bytes.fromhex(digests[r["id"]]) for r in rows],
            "objects": [links[digests[r["id"]]][0] for r in rows],
            "sizes": [links[digests[r["id"]]][1] for r in rows],
            "chars": [len(r["content"]) for r in rows],
        },
    )


def inline_count() -> int:
    """Rows whose text is still inline: an index-only count of
    idx_page_fetches_inline_text."""
    row = db.query_one(f"SELECT count(*) AS n FROM page_fetches WHERE {INLINE}")
    return row["n"] if row else 0


async def handle_tier_page_text(task_id: int, payload: dict[str, Any]) -> None:
    behind = await asyncio.to_thread(data_level.behind, data_level.LEVEL)
    if behind:
        set_progress(task_id, 0, 0, f"waiting for {behind} older workers", extra={"behind": behind})
        return
    total = await asyncio.to_thread(inline_count)
    moved = 0
    while db.get_config("page_text_move_enabled") and not cancelled(task_id):
        n = await asyncio.to_thread(move)
        if not n:
            break
        moved += n
        # Fetches stored inline during the run can carry it past the count.
        total = max(total, moved)
        set_progress(task_id, moved, total, f"moved {moved} page texts to object storage")
    set_progress(
        task_id,
        moved,
        moved,
        f"moved {moved} page texts to object storage",
        extra={"moved": moved},
    )
    logger.info("tier_page_text: moved %s", moved)
