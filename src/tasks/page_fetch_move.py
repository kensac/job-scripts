"""Moves the page fetches still stored in ai_queries into page_fetch_rows.

Idempotent by predicate: a fetch qualifies while it is still a content row in
ai_queries, a url qualifies while its only page text is a copy on an answer.
A second run finds nothing, and a run cut short resumes where the rows are.

Each fetch keeps its id (both tables draw from ai_queries_id_seq), so every
content_row_id that named it still names it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from core.store import MIN_CONTENT_CHARS
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

# Rows per statement. 2.6 GB of text over 910,622 rows on 2026-10-09 is about
# 2.9 MB a batch: short row locks, and a cancelled run loses one batch.
BATCH = 1000

# One statement, so a fetch is in exactly one table at every moment: readers of
# the page_fetches view see it once whichever snapshot they hold. The rows are
# locked in id order, the only order any writer locks them in. Fetches written
# with no recorded origin ('content cached', 6,188 rows, all from before
# 2026-08-26) get one honest label.
_MOVE = """
    WITH batch AS (
        SELECT id FROM ai_queries
        WHERE check_type = 'content' AND id > %(after)s
        ORDER BY id LIMIT %(limit)s
        FOR UPDATE
    ), moved AS (
        DELETE FROM ai_queries a USING batch WHERE a.id = batch.id
        RETURNING a.id, a.url, a.status, a.reason, a.input_content, a.worker, a.created_at
    )
    INSERT INTO page_fetch_rows (id, url, status, method, content, worker, created_at)
    SELECT id, url, status,
           CASE WHEN reason IS NULL OR reason = 'content cached' THEN 'unknown' ELSE reason END,
           input_content, worker, created_at
    FROM moved
    RETURNING id
"""

# Older verification fetched the page itself and kept the text only on its
# closed and clearance answers: 11,189 urls on 2026-10-10, every copy older
# than id 91,749. Readers pick a url's text two ways, and each pick on such a
# url becomes a fetch with the answer's id, so neither answer changes when the
# copies stop being read:
#   - get_content(s): the newest text.
#   - CONTENT_LATERAL: the newest text longer than MIN_CONTENT_CHARS
#     (a different row on 83 of those urls).
# The other copies (11,894 rows) stay copies. Of the derivations that name a
# copy, every one whose stored hash matches the text a reader picks names the
# picked row itself (4,794 embeddings, 4,862 requirements, 29 profiles), so it
# names a fetch once this runs. The rest match no text a reader picks, and
# their sweeps re-derive them whichever row they name.
# A url whose fetched text exists is left alone: CONTENT_LATERAL already
# prefers fetched text, and on the 4,856 urls with a copy newer than their
# newest fetch the two texts were equal on all but one, two fetches a second
# apart of the same page.
_CONVERT = f"""
    WITH copy_only AS (
        SELECT url FROM page_texts GROUP BY url HAVING bool_and(on_verdict)
    ), picked AS (
        SELECT (SELECT t.id FROM page_texts t WHERE t.url = c.url
                ORDER BY t.id DESC LIMIT 1) AS id
        FROM copy_only c
        UNION
        SELECT (SELECT t.id FROM page_texts t WHERE t.url = c.url
                  AND length(t.input_content) > {MIN_CONTENT_CHARS}
                ORDER BY t.id DESC LIMIT 1)
        FROM copy_only c
    )
    INSERT INTO page_fetch_rows (id, url, status, method, content, worker, created_at)
    SELECT a.id, a.url, 'passed', 'verification', a.input_content, a.worker, a.created_at
    FROM ai_queries a JOIN picked p ON p.id = a.id
    RETURNING id
"""


def move_batch(after: int, limit: int = BATCH) -> list[int]:
    return sorted(r["id"] for r in db.query(_MOVE, {"after": after, "limit": limit}))


def convert_copies() -> int:
    return len(db.query(_CONVERT))


def remaining() -> int:
    row = db.query_one("SELECT count(*) AS n FROM ai_queries WHERE check_type = 'content'")
    return int(row["n"]) if row else 0


async def handle_move_page_fetches(task_id: int, payload: dict[str, Any]) -> None:
    converted = await asyncio.to_thread(convert_copies)
    total = await asyncio.to_thread(remaining)
    moved, after = 0, 0
    set_progress(task_id, 0, total, "moving page fetches")
    while not cancelled(task_id):
        ids = await asyncio.to_thread(move_batch, after)
        if not ids:
            break
        moved += len(ids)
        after = ids[-1]
        set_progress(task_id, moved, total, "moving page fetches")
    extra = {"moved": moved, "converted": converted}
    set_progress(task_id, moved, total, f"moved {moved} fetches, converted {converted}", extra)
    logger.info("move_page_fetches: %s", extra)
