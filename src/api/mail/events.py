"""The one writer of `email_events`.

The log is append-only and the newest row per message is the one in force.
`append` writes the row and moves `email_messages.current_event_id` to it in
one statement, so the pointer can never name an older row than the log holds.
`tests/test_mail_pointers.py` fails on an INSERT into the table anywhere else.
"""

from __future__ import annotations

import datetime
from typing import Any

from api import db

# Who wrote an event: `model` names the machine, `actor_user_id` the person.
# A rule that needs no model still names itself, because both NULL reads as
# "nobody wrote this" (api/orm/mail.py).
SELF_SENT_RULE = "rule:self_sent"

# GREATEST, not the new id outright: two appends to one message can commit in
# either order, and the pointer must end on the newer row whichever lands last.
# The second UPDATE waits on the first's row lock and re-reads the row, so it
# compares against the committed pointer.
_APPEND = """
WITH appended AS (
    INSERT INTO email_events (message_id, kind, confidence, occurred_at, deadline_at,
                              deadline_inferred, detail, model, actor_user_id)
    VALUES (%(message_id)s, %(kind)s, %(confidence)s, %(occurred_at)s, %(deadline_at)s,
            %(deadline_inferred)s, %(detail)s, %(model)s, %(actor_user_id)s)
    RETURNING id, message_id
), pointed AS (
    UPDATE email_messages m
    SET current_event_id = GREATEST(m.current_event_id, a.id)
    FROM appended a WHERE m.id = a.message_id
)
SELECT id FROM appended
"""


def append(
    message_id: int,
    kind: str,
    *,
    confidence: str | None,
    detail: dict[str, Any] | None,
    model: str | None = None,
    actor_user_id: int | None = None,
    occurred_at: datetime.datetime | None = None,
    deadline_at: datetime.datetime | None = None,
    deadline_inferred: bool = False,
) -> int:
    row = db.query_one(
        _APPEND,
        {
            "message_id": message_id,
            "kind": kind,
            "confidence": confidence,
            "occurred_at": occurred_at,
            "deadline_at": deadline_at,
            "deadline_inferred": deadline_inferred,
            "detail": db.jsonb(detail) if detail is not None else None,
            "model": model,
            "actor_user_id": actor_user_id,
        },
    )
    assert row is not None
    return row["id"]
