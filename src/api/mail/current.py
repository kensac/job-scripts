"""The current event and the current match of a message.

`email_events` and `application_matches` are append-only, and the newest row
per message is the one in force: a reclassification retracts the old kind and
a rematch moves the message off its old application. Its writer stores the
newest id on the message (`email_messages.current_event_id`,
`current_match_id`), so the current row is a join, not a pass over the log.

A statement that already has the message row joins through the pointer
itself (`JOIN email_events e ON e.id = m.current_event_id`), as the resolve
queue does. One that wants the current rows of many messages without one
takes its subquery from here. `tests/test_mail_current.py` fails on a
"newest row per message" written anywhere else, and holds this module equal
to it.

Measured on production 2026-10-10 after the fill (69,318 messages, 83,846
events, 15,151 matches), medians of six: the resolve queue 163 ms through the
pointers against 231 ms for the newest-row pass; the funnel 94 ms against
105 ms; a whole-mailbox count by current kind 144 ms against 267 ms. Joining
the queue on both the message id and the pointer made the planner expect one
row and fall back to nested loops (340 ms), so it joins on the pointer alone.
"""

from __future__ import annotations

from api import db

# Per message, the newest id of each log beside the pointer meant to hold it.
# The pointers are set by the writers (`events.append`, `match.record`); this
# is what checks them, and what the backfill fills them from.
NEWEST_IDS = """
SELECT m.id AS message_id, m.current_event_id, m.current_match_id,
       (SELECT max(e.id) FROM email_events e WHERE e.message_id = m.id) AS event_id,
       (SELECT max(am.id) FROM application_matches am WHERE am.message_id = m.id) AS match_id
FROM email_messages m
"""


def stale_pointers() -> int:
    """Messages whose pointer is not the newest row of its log."""
    row = db.query_one(
        f"""
        SELECT count(*) AS n FROM ({NEWEST_IDS}) p
        WHERE p.current_event_id IS DISTINCT FROM p.event_id
           OR p.current_match_id IS DISTINCT FROM p.match_id
        """
    )
    return int(row["n"]) if row else 0


def _through(pointer: str, table: str, columns: tuple[str, ...]) -> str:
    listed = ", ".join(("p.id AS message_id", *(f"r.{c}" for c in columns)))
    return f"SELECT {listed} FROM email_messages p JOIN {table} r ON r.id = p.{pointer}"


def current_event(*columns: str) -> str:
    """The newest `email_events` row per message: `message_id` and `columns`."""
    return _through("current_event_id", "email_events", columns)


def current_match(*columns: str) -> str:
    """The newest `application_matches` row per message: `message_id` and
    `columns`. A null `application_id` is a recorded outcome, not an absence."""
    return _through("current_match_id", "application_matches", columns)
