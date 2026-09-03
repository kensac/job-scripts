"""Recover the markup from bodies that were stored as raw HTML.

Ordering is the whole point of this module and it is why the copy and the
derive live in one statement rather than two.

Before `body_html` existed the import wrote raw markup into `body_text` on 96%
of the .olm corpus, and `body_text` is the ONLY copy - the import streams the
archive and retains nothing, and no other column in the schema holds it.
Deriving the text first would therefore have destroyed the markup permanently,
including for the messages the reader exists to render. The UPDATE below
writes both fields from the same source row in one pass, so there is no window
in which the markup is gone and no way to run the halves out of order.

Idempotent by predicate rather than by bookkeeping: a row qualifies only while
its body still holds markup and its html is still empty, so a second run
selects nothing and a partial run resumes exactly where it stopped.
"""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.tasks.runtime import _cancelled, _set_progress
from core.mail_import import MAX_BODY_CHARS, MAX_HTML_CHARS, _html_to_text

logger = logging.getLogger("jobtracker_worker")

# Matches the import's own rule so a backfilled row is indistinguishable from a
# freshly imported one. A body holds markup when it contains a closing tag;
# 27,221 of 28,451 .olm bodies contain a "<" and 27,198 contain a closing tag,
# so the 23 that differ are plain text using "<" legitimately.
_PENDING = """
    source IS NOT NULL
    AND body_html IS NULL
    AND body_text IS NOT NULL
    AND body_text ~* '</[a-z][a-z0-9]*>'
"""

# Rows per transaction. Small enough that a cancelled or crashed run loses at
# most this much work and the table is never locked for long, large enough that
# 27k rows do not become 27k round trips.
_CHUNK = 500


def pending_count() -> int:
    row = db.query_one(f"SELECT count(*) AS n FROM email_messages WHERE {_PENDING}")
    return int(row["n"]) if row else 0


def _convert_chunk(limit: int) -> int:
    rows = db.query(
        f"SELECT id, body_text FROM email_messages WHERE {_PENDING} ORDER BY id LIMIT %s",
        (limit,),
    )
    if not rows:
        return 0
    converted = [
        (
            _html_to_text(row["body_text"])[:MAX_BODY_CHARS] or None,
            row["body_text"][:MAX_HTML_CHARS],
            row["id"],
        )
        for row in rows
    ]
    with db.pool.connection() as conn, conn.cursor() as cur:
        # One statement per row, one transaction per chunk: body_html is
        # written from the SAME value body_text is derived from, so the markup
        # is preserved before the text that contained it is replaced.
        cur.executemany(
            "UPDATE email_messages SET body_text = %s, body_html = %s WHERE id = %s",
            converted,
        )
    return len(rows)


async def handle_backfill_message_html(task_id: int, payload: dict[str, Any]) -> None:
    total = pending_count()
    _set_progress(task_id, 0, total, "recovering markup")
    done = 0
    while True:
        if _cancelled(task_id):
            logger.info("Task %s cancelled after %s messages", task_id, done)
            return
        moved = _convert_chunk(int(payload.get("chunk") or _CHUNK))
        if not moved:
            break
        done += moved
        _set_progress(task_id, done, total, "recovering markup")
    _set_progress(task_id, total, total, f"recovered markup on {done} messages")
    logger.info("backfill_message_html: %s messages", done)
