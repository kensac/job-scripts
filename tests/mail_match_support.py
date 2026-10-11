from __future__ import annotations

import itertools

from api import db
from tests import mail_log

_seq = itertools.count(1)


def _message(user_id: int, *, subject="s", body="b", sent_at=None, thread=None) -> int:
    row = db.query_one(
        """
        INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id,
                                    source, subject, body_text, sent_at)
        VALUES (%s, %s, %s, 'takeout', %s, %s, %s) RETURNING id
        """,
        (user_id, f"msg-{next(_seq)}", thread, subject, body, sent_at),
    )
    assert row is not None
    return row["id"]


def _application(
    user_id: int, *, company=None, title=None, applied_at=None, provenance="email"
) -> int:
    row = db.query_one(
        """
        INSERT INTO applications
            (user_id, job_id, company_name, title, applied_at, source_provenance)
        VALUES (%s, NULL, %s, %s, %s, %s) RETURNING id
        """,
        (user_id, company, title, applied_at, provenance),
    )
    assert row is not None
    return row["id"]


def _event(message_id: int, kind: str, *, company=None, title=None) -> int:
    detail = {}
    if company:
        detail["company"] = company
    if title:
        detail["role_title"] = title
    row = mail_log.query_one(
        "INSERT INTO email_events (message_id, kind, confidence, detail) "
        "VALUES (%s, %s, 'high', %s) RETURNING id",
        (message_id, kind, db.jsonb(detail)),
    )
    assert row is not None
    return row["id"]
