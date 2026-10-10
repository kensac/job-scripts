"""Raw inserts into the two mail logs, for fixtures, with the message's
pointer moved the way the one writer of each log moves it.

A fixture that writes `email_events` or `application_matches` directly would
leave `email_messages.current_event_id` / `current_match_id` behind, and every
reader of the current row joins through those pointers. These wrappers take
the fixture's own INSERT unchanged and move the pointer in the same
statement, so a test can still write any column it needs to.
"""

from __future__ import annotations

import re
from typing import Any

from api import db

_POINTER = {"email_events": "current_event_id", "application_matches": "current_match_id"}


def _wrap(sql: str) -> str:
    table = re.search(r"INSERT\s+INTO\s+(\w+)", sql, re.IGNORECASE)
    assert table and table.group(1) in _POINTER, sql
    column = _POINTER[table.group(1)]
    body = re.sub(r"\s+RETURNING\s+[\w\s,]+$", "", sql.strip(), flags=re.IGNORECASE)
    return f"""
    WITH ins AS ({body} RETURNING *),
    pointed AS (
        UPDATE email_messages m SET {column} = GREATEST(m.{column}, n.id)
        FROM (SELECT message_id, max(id) AS id FROM ins GROUP BY message_id) n
        WHERE m.id = n.message_id
    )
    SELECT * FROM ins ORDER BY id
    """


def execute(sql: str, params: Any = None) -> None:
    db.query(_wrap(sql), params)


def query(sql: str, params: Any = None) -> list[dict[str, Any]]:
    return db.query(_wrap(sql), params)


def query_one(sql: str, params: Any = None) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None
