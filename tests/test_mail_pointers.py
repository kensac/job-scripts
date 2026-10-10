"""`email_messages.current_event_id` and `current_match_id`: the newest row of
each log, set by its one writer in the statement that appends."""

from __future__ import annotations

import pathlib
import re

from api import db
from api.health.mail import _detect_mail
from api.mail import current, events
from api.mail import match as mail_match
from tasks import mail_pointers

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def _message(user_id: int, n: int) -> int:
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source) "
        "VALUES (%s, %s, 'gmail') RETURNING id",
        (user_id, f"<p{n}@x>"),
    )
    assert row is not None
    return row["id"]


def _pointers(message_id: int) -> tuple[int | None, int | None]:
    row = db.query_one(
        "SELECT current_event_id, current_match_id FROM email_messages WHERE id = %s",
        (message_id,),
    )
    assert row is not None
    return row["current_event_id"], row["current_match_id"]


def _newest(table: str, message_id: int) -> int:
    row = db.query_one(f"SELECT max(id) AS id FROM {table} WHERE message_id = %s", (message_id,))
    assert row is not None
    return row["id"]


def test_each_writer_moves_its_pointer_in_the_same_statement(f):
    mid = _message(f.make_user(), 1)
    first = events.append(mid, "acknowledgement", confidence="high", detail={}, model="m")
    assert _pointers(mid) == (first, None)
    second = events.append(mid, "rejection", confidence="high", detail={}, model="m")
    assert _pointers(mid) == (second, None)

    mail_match.record(mid, mail_match.Match(None, mail_match.UNMATCHED, "none", "nothing"))
    assert _pointers(mid) == (second, _newest("application_matches", mid))
    assert current.stale_pointers() == 0


def test_a_pointer_never_moves_back_to_an_older_row(f):
    mid = _message(f.make_user(), 2)
    older = events.append(mid, "acknowledgement", confidence="high", detail={}, model="m")
    newer = events.append(mid, "rejection", confidence="high", detail={}, model="m")
    # A late commit of the older append re-runs its UPDATE against the newer pointer.
    db.execute(
        "UPDATE email_messages SET current_event_id = GREATEST(current_event_id, %s) WHERE id = %s",
        (older, mid),
    )
    assert _pointers(mid)[0] == newer


def test_the_backfill_fills_what_the_writers_never_saw_and_then_finds_nothing(f):
    uid = f.make_user()
    mids = [_message(uid, 10 + i) for i in range(3)]
    # Rows written before the pointers existed: straight into the logs.
    for mid in mids[:2]:
        for kind in ("acknowledgement", "rejection"):
            db.execute(
                "INSERT INTO email_events (message_id, kind, model) VALUES (%s, %s, 'm')",
                (mid, kind),
            )
    db.execute(
        "INSERT INTO application_matches (message_id, method) VALUES (%s, 'unmatched')",
        (mids[0],),
    )
    assert current.stale_pointers() == 2

    last, filled = mail_pointers.fill_batch(0, limit=2)
    assert (last, filled) == (mids[1], 2)
    assert _pointers(mids[0]) == (
        _newest("email_events", mids[0]),
        _newest("application_matches", mids[0]),
    )
    assert _pointers(mids[1]) == (_newest("email_events", mids[1]), None)
    assert mail_pointers.fill_batch(mids[1], limit=2) == (mids[2], 0)
    assert mail_pointers.fill_batch(mids[2], limit=2) == (None, 0)
    assert mail_pointers.fill_batch(0) == (mids[2], 0)
    assert current.stale_pointers() == 0


def test_the_self_sent_rule_is_named_and_nothing_else_is(f):
    uid = f.make_user()
    mid = _message(uid, 20)
    db.execute(
        "INSERT INTO email_events (message_id, kind, detail) VALUES "
        "(%s, 'not_job_related', '{\"reason\": \"self_sent\"}'), "
        '(%s, \'not_job_related\', \'{"reason": "self_sent", "superseded": true}\'), '
        "(%s, 'rejection', '{}')",
        (mid, mid, mid),
    )
    db.execute(
        "INSERT INTO email_events (message_id, kind, detail, actor_user_id) "
        "VALUES (%s, 'not_job_related', '{\"reason\": \"self_sent\"}', %s)",
        (mid, uid),
    )
    assert mail_pointers.name_the_rule() == 2
    assert mail_pointers.name_the_rule() == 0
    rows = db.query("SELECT model FROM email_events WHERE message_id = %s ORDER BY id", (mid,))
    assert [r["model"] for r in rows] == [events.SELF_SENT_RULE] * 2 + [None, None]


def test_a_message_deletes_with_its_logs_despite_the_pointers(f):
    mid = _message(f.make_user(), 30)
    events.append(mid, "rejection", confidence="high", detail={}, model="m")
    mail_match.record(mid, mail_match.Match(None, mail_match.UNMATCHED, "none", "nothing"))
    db.execute("DELETE FROM email_messages WHERE id = %s", (mid,))
    assert db.query_one("SELECT 1 FROM email_events WHERE message_id = %s", (mid,)) is None


def test_lag_after_a_finished_backfill_is_an_alert(f):
    mid = _message(f.make_user(), 40)
    db.execute(
        "INSERT INTO email_events (message_id, kind, model) VALUES (%s, 'offer', 'm')", (mid,)
    )
    assert _detect_mail() == [], "before any backfill, lag is expected"
    f.make_task("backfill_mail_pointers", {}, status="done")
    found = _detect_mail()
    assert [(a["kind"], a["detail"]["stale"]) for a in found] == [("mail_pointer_stale", 1)]
    f.make_task("backfill_mail_pointers", {}, status="pending")
    assert _detect_mail() == [], "quiet while a backfill is queued"


def test_each_log_has_one_writer():
    writers = {
        "email_events": _SRC / "api" / "mail" / "events.py",
        "application_matches": _SRC / "api" / "mail" / "match.py",
    }
    found = [
        f"{table}: {path.relative_to(_SRC)}"
        for path in sorted(_SRC.rglob("*.py"))
        for table, owner in writers.items()
        if path != owner
        and re.search(rf"INSERT\s+INTO\s+{table}\b", path.read_text(), re.IGNORECASE)
    ]
    assert not found, "append through mail.events.append or mail.match.record: " + ", ".join(found)
