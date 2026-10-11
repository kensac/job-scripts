"""Find finished matcher sweeps and changes that warrant another one."""

from __future__ import annotations

import datetime

from api import db


def last_sweep_start(user_id: int | None = None) -> datetime.datetime | None:
    """When the newest finished sweep that covered this user (or, with no
    user, everybody) started. None when there has never been one.

    Its start, not its finish: anything written while it ran may have landed
    after it read, so it counts as new to the next sweep. A run with a
    `limit` decided only part of the mail and does not count."""
    row = db.query_one(
        """
        SELECT started_at FROM tasks
        WHERE kind = 'match_mail' AND status = 'done' AND started_at IS NOT NULL
          AND payload->>'limit' IS NULL
          AND (payload->>'user_id' IS NULL OR payload->>'user_id' = %(user)s::text)
        ORDER BY id DESC LIMIT 1
        """,
        {"user": user_id},
    )
    return row["started_at"] if row else None


# What a sweep reads that can change its answer: a new application (a
# candidate that was missing), a new event (a reclassified message), a new
# match (a person's decision, or a sweep's own). A board row moving into
# an applied status creates its application as it is written. Nothing else
# does, so a sweep after none of these would decide everything exactly as the
# last one.
_CHANGED_SINCE = """
SELECT EXISTS (
           SELECT 1 FROM applications
           WHERE created_at > %(since)s AND (%(user)s::bigint IS NULL OR user_id = %(user)s))
    OR EXISTS (
           SELECT 1 FROM email_events e JOIN email_messages m ON m.id = e.message_id
           WHERE e.created_at > %(since)s AND (%(user)s::bigint IS NULL OR m.user_id = %(user)s))
    OR EXISTS (
           SELECT 1 FROM application_matches am JOIN email_messages m ON m.id = am.message_id
           WHERE am.created_at > %(since)s AND (%(user)s::bigint IS NULL OR m.user_id = %(user)s))
    AS changed
"""


def changed_since(since: datetime.datetime | None, user_id: int | None = None) -> bool:
    if since is None:
        return True
    row = db.query_one(
        _CHANGED_SINCE,
        {"since": since, "user": user_id},
    )
    return bool(row and row["changed"])
