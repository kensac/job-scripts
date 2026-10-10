"""The .olm copy of a message Takeout also holds is removed, keeping the
history only the .olm copy had, and .olm ids gain the header's brackets."""

from __future__ import annotations

from api import db
from api.mail import current, events
from api.mail import match as mail_match
from core.mail.importer import _bracketed
from tasks import mail_olm_twins


def _message(user_id: int, source: str, message_id: str) -> int:
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source) "
        "VALUES (%s, %s, %s) RETURNING id",
        (user_id, message_id, source),
    )
    assert row is not None
    return row["id"]


def _event(message_id: int, kind: str) -> int:
    return events.append(message_id, kind, confidence="high", detail={}, model="m")


def test_a_twin_goes_and_only_its_unshared_history_moves(f):
    uid = f.make_user()
    app = db.query_one(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, 'Acme', 'email') RETURNING id",
        (uid,),
    )["id"]
    # Classified and attached only through the .olm copy.
    olm_a, keep_a = _message(uid, "olm", "a@x"), _message(uid, "takeout", "<a@x>")
    moved_event = _event(olm_a, "rejection")
    mail_match.record(olm_a, mail_match.Match(app, mail_match.ATS_COMPANY, "high", "seen"))
    # Both copies classified: the kept copy's answer stands.
    olm_b, keep_b = _message(uid, "olm", "b@x"), _message(uid, "takeout", "<b@x>")
    _event(olm_b, "offer")
    kept_event = _event(keep_b, "acknowledgement")
    lone = _message(uid, "olm", "c@x")
    fallback = _message(uid, "olm", "olm-archive-7")

    counts = mail_olm_twins.merge()

    assert counts == {"removed": 2, "moved_events": 1, "moved_matches": 1, "bracketed": 1}
    left = {r["id"] for r in db.query("SELECT id FROM email_messages WHERE user_id = %s", (uid,))}
    assert left == {keep_a, keep_b, lone, fallback}
    pointers = db.query(
        "SELECT id, current_event_id, current_match_id FROM email_messages WHERE id = ANY(%s) "
        "ORDER BY id",
        ([keep_a, keep_b],),
    )
    assert pointers[0]["current_event_id"] == moved_event
    assert pointers[0]["current_match_id"] is not None
    assert pointers[1]["current_event_id"] == kept_event
    assert current.stale_pointers() == 0
    ids = dict(
        (r["id"], r["provider_message_id"])
        for r in db.query("SELECT id, provider_message_id FROM email_messages")
    )
    assert ids[lone] == "<c@x>" and ids[fallback] == "olm-archive-7"

    assert mail_olm_twins.merge() == {
        "removed": 0,
        "moved_events": 0,
        "moved_matches": 0,
        "bracketed": 0,
    }


def test_the_importer_brackets_an_olm_id():
    assert _bracketed("a@x") == "<a@x>"
    assert _bracketed("<a@x>") == "<a@x>"
    assert _bracketed(None) is None
