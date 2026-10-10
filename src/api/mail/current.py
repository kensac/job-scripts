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


def _newest(table: str, columns: tuple[str, ...]) -> str:
    return (
        f"SELECT DISTINCT ON (message_id) {', '.join(('message_id', *columns))} "
        f"FROM {table} ORDER BY message_id, id DESC"
    )


def current_event(*columns: str) -> str:
    """The newest `email_events` row per message: `message_id` and `columns`."""
    return _newest("email_events", columns)


def current_match(*columns: str) -> str:
    """The newest `application_matches` row per message: `message_id` and
    `columns`. A null `application_id` is a recorded outcome, not an absence."""
    return _newest("application_matches", columns)
