"""Removes the .olm copies of messages Takeout also holds, and brackets every
.olm Message-ID the way the header and every other source spell it.

Outlook's export drops the angle brackets, so 227 messages imported from both
.olm and Takeout were stored twice (2026-10-10). Takeout's copy is kept: it
carries the threading headers and the thread id, and .olm carries neither.
No person wrote an event or a match on any .olm twin (checked that day).

A twin's log moves to the kept copy only where the kept copy has none of
that log, so a message classified or matched only through its .olm copy
(9 and 2 of them) keeps that history. Where both copies have a log, the
kept copy's stands: on 6 of 13 doubly matched pairs the two disagree about
the application, and the Takeout copy is the one kept. Everything runs in
one transaction, and a second run finds no twin and no unbracketed id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from api.mail import current
from tasks.runtime import set_progress

logger = logging.getLogger(__name__)

# The importer's stand-in for a message with no Message-ID
# (core.mail.importer._olm_entries), which is not a header value to bracket.
_FALLBACK = "olm-%"

_PAIRS = """
    SELECT o.id AS olm_id, t.id AS keep_id
    FROM email_messages o
    JOIN email_messages t
      ON t.user_id = o.user_id AND t.provider_message_id = '<' || o.provider_message_id || '>'
    WHERE o.source = 'olm' AND o.provider_message_id NOT LIKE '<%%'
"""


def _move(table: str) -> int:
    return db.execute_count(
        f"""
        UPDATE {table} x SET message_id = p.keep_id
        FROM ({_PAIRS}) p
        WHERE x.message_id = p.olm_id
          AND NOT EXISTS (SELECT 1 FROM {table} k WHERE k.message_id = p.keep_id)
        """
    )


def merge() -> dict[str, int]:
    with db.transaction():
        keep = [r["keep_id"] for r in db.query(f"SELECT keep_id FROM ({_PAIRS}) p ORDER BY 1")]
        moved_events = _move("email_events")
        moved_matches = _move("application_matches")
        db.execute(
            f"""
            UPDATE email_messages m
            SET current_event_id = GREATEST(m.current_event_id, n.event_id),
                current_match_id = GREATEST(m.current_match_id, n.match_id)
            FROM ({current.NEWEST_IDS} WHERE m.id = ANY(%(keep)s)) n
            WHERE m.id = n.message_id
            """,
            {"keep": keep},
        )
        removed = db.execute_count(
            f"DELETE FROM email_messages WHERE id IN (SELECT olm_id FROM ({_PAIRS}) p)"
        )
        bracketed = db.execute_count(
            "UPDATE email_messages SET provider_message_id = '<' || provider_message_id || '>' "
            "WHERE source = 'olm' AND provider_message_id NOT LIKE '<%%' "
            "AND provider_message_id NOT LIKE %s",
            (_FALLBACK,),
        )
    return {
        "removed": removed,
        "moved_events": moved_events,
        "moved_matches": moved_matches,
        "bracketed": bracketed,
    }


async def handle_merge_olm_twins(task_id: int, payload: dict[str, Any]) -> None:
    counts = await asyncio.to_thread(merge)
    label = (
        f"removed {counts['removed']} .olm twins, moved {counts['moved_events']} events and "
        f"{counts['moved_matches']} matches to the kept copy, bracketed {counts['bracketed']} ids"
    )
    set_progress(task_id, 1, 1, label, counts)
    logger.info("merge_olm_twins: %s", counts)
