"""Persisting imported messages, and the prefilter verdict that rides along.

Dedupe is on (user_id, provider_message_id). It has to be, because the same
message arrives repeatedly: the four .olm archives overlap with each other and
with Takeout, Takeout overlaps live Gmail, and a re-run of any import replays
everything it already did.

`ON CONFLICT DO UPDATE` rather than DO NOTHING, but only for the fields a
better copy can improve. A later source may carry a body an earlier one
lacked; nothing may overwrite a body with NULL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from itertools import islice

from api import db
from core.mail_import import ImportedMessage
from core.mail_prefilter import looks_job_related

logger = logging.getLogger("jobtracker_api")

# Rows per INSERT. Large enough that 38,685 messages is ~39 statements rather
# than 38,685 round trips, small enough that one failed batch re-does little
# and the parameter count stays far below Postgres's 65535 limit (13 columns
# here, so the ceiling is ~5000 rows).
INSERT_CHUNK = 1000


def _chunks(items: Iterable[ImportedMessage], size: int) -> Iterator[list[ImportedMessage]]:
    iterator = iter(items)
    while chunk := list(islice(iterator, size)):
        yield chunk


def store_messages(user_id: int, messages: Iterable[ImportedMessage]) -> int:
    """Insert or improve rows, returning how many were written.

    The prefilter runs here rather than at classification time so its verdict
    is recorded against the message as imported. It gates nothing - every
    message is classified regardless - but ordering the sweep and measuring
    what a gate would have missed both need the verdict stored, not recomputed
    later against rules that may since have changed.
    """
    written = 0
    for chunk in _chunks(messages, INSERT_CHUNK):
        rows = []
        for message in chunk:
            verdict = looks_job_related(
                from_email=message.from_email,
                subject=message.subject,
                body=message.body_text,
            )
            rows.append(
                (
                    user_id,
                    message.provider_message_id,
                    message.provider_thread_id,
                    message.source,
                    message.from_email,
                    message.from_name,
                    message.to_emails,
                    message.subject,
                    message.sent_at,
                    message.body_text,
                    verdict.hit,
                    verdict.reason,
                    db.jsonb(message.headers) if message.headers else None,
                )
            )
        with db.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO email_messages (
                    user_id, provider_message_id, provider_thread_id, source,
                    from_email, from_name, to_emails, subject, sent_at,
                    body_text, prefilter_hit, prefilter_reason, headers
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, provider_message_id) DO UPDATE SET
                    -- Only ever improve. A thinner copy from another archive
                    -- must not blank a field an earlier import already filled.
                    body_text = COALESCE(EXCLUDED.body_text, email_messages.body_text),
                    subject = COALESCE(EXCLUDED.subject, email_messages.subject),
                    sent_at = COALESCE(EXCLUDED.sent_at, email_messages.sent_at),
                    provider_thread_id = COALESCE(
                        email_messages.provider_thread_id, EXCLUDED.provider_thread_id
                    ),
                    prefilter_hit = EXCLUDED.prefilter_hit,
                    prefilter_reason = EXCLUDED.prefilter_reason,
                    -- Same only-ever-improve rule: an archive that carried the
                    -- threading chain must not be blanked by one that did not.
                    headers = COALESCE(EXCLUDED.headers, email_messages.headers)
                """,
                rows,
            )
            written += len(rows)
    return written


def counts(user_id: int) -> dict[str, int]:
    row = db.query_one(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE prefilter_hit) AS prefilter_hits,
               COUNT(*) FILTER (WHERE body_text IS NULL) AS bodyless,
               COUNT(DISTINCT source) AS sources
        FROM email_messages WHERE user_id = %s
        """,
        (user_id,),
    )
    return dict(row) if row else {}
