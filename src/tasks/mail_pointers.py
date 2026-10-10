"""Fills `email_messages.current_event_id` and `current_match_id`.

The writers set both pointers from the release that added them. This brings
every older message up to the same state, and names the rule that wrote the
self-sent corrections with no model.

Idempotent by predicate: a message is touched only while a pointer differs
from the newest id of its log, so a second run finds nothing and a run cut
short resumes where the rows are.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from api.mail import current, events
from tasks.runtime import cancelled, set_progress

logger = logging.getLogger(__name__)

# Messages per statement: 69,280 messages on 2026-10-10 is 70 statements, each
# locking at most a thousand rows for a few milliseconds.
BATCH = 1000

# GREATEST, because a pointer can only ever be behind its log, never ahead:
# an append that commits while this runs has already moved the pointer past
# the newest id this statement's snapshot saw, and must not be moved back.
_FILL = f"""
WITH chunk AS (
    SELECT id FROM email_messages WHERE id > %(after)s ORDER BY id LIMIT %(limit)s
), newest AS (
    {current.NEWEST_IDS} WHERE m.id IN (SELECT id FROM chunk)
), filled AS (
    UPDATE email_messages m
    SET current_event_id = GREATEST(m.current_event_id, n.event_id),
        current_match_id = GREATEST(m.current_match_id, n.match_id)
    FROM newest n
    WHERE m.id = n.message_id
      AND (m.current_event_id IS DISTINCT FROM n.event_id
           OR m.current_match_id IS DISTINCT FROM n.match_id)
    RETURNING m.id
)
SELECT (SELECT max(id) FROM chunk) AS last, (SELECT count(*) FROM filled) AS filled
"""

# Both corrections were written with no model and no person, which
# api/orm/mail.py calls a bug: 1,028 rows on 2026-10-10, every one of them
# `not_job_related` with reason `self_sent`.
_NAME_THE_RULE = """
UPDATE email_events SET model = %(rule)s
WHERE model IS NULL AND actor_user_id IS NULL AND detail->>'reason' = 'self_sent'
"""


def fill_batch(after: int, limit: int = BATCH) -> tuple[int | None, int]:
    row = db.query_one(_FILL, {"after": after, "limit": limit})
    assert row is not None
    return row["last"], int(row["filled"])


def name_the_rule() -> int:
    return db.execute_count(_NAME_THE_RULE, {"rule": events.SELF_SENT_RULE})


async def handle_backfill_mail_pointers(task_id: int, payload: dict[str, Any]) -> None:
    named = await asyncio.to_thread(name_the_rule)
    total = await asyncio.to_thread(current.stale_pointers)
    filled, after = 0, 0
    set_progress(task_id, 0, total, "filling current event and match pointers")
    while not cancelled(task_id):
        last, n = await asyncio.to_thread(fill_batch, after)
        if last is None:
            break
        filled += n
        after = last
        set_progress(task_id, filled, total, "filling current event and match pointers")
    extra = {"filled": filled, "named": named}
    set_progress(task_id, filled, total, f"filled {filled} messages, named {named} events", extra)
    logger.info("backfill_mail_pointers: %s", extra)
