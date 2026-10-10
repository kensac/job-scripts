"""The current event and the current match of a message.

`email_events` and `application_matches` are append-only, and the newest row
per message is the one in force: a reclassification retracts the old kind and
a rematch moves the message off its old application. Every statement that
reads "the current one" for many messages takes its subquery from here, and
`tests/test_mail_current.py` fails on a copy written anywhere else. One
message's current match is `match.latest`, an index probe.

The caller names the columns it reads, rather than this module selecting
every column, because of how Postgres plans the two shapes (measured on
production 2026-10-10, 83,846 events, 15,151 matches). Referenced once, the
subquery is inlined and unused columns are dropped, so a wide list costs
nothing: about 200 ms either way. Referenced twice, as the resolve queue
does, a CTE is materialized whole: the queue measured a median 227 ms with
its narrow lists against 297 ms with every column. A database view was
rejected for the same reason: each reference to it is planned separately,
so the queue paid for the pass twice, a median 299 ms.
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
