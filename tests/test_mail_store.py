"""Storing imported messages, and what happens when the same one arrives twice.

It will arrive twice. The four .olm archives overlap with each other and with
Takeout, Takeout overlaps live Gmail, and re-running any import replays
everything. Dedupe is not a nicety here, it is the normal path.
"""

from __future__ import annotations

import datetime

from api import db, mail_store
from core.mail_import import ImportedMessage


def _msg(**kw) -> ImportedMessage:
    base = {
        "provider_message_id": "<m1@x>",
        "source": "takeout",
        "from_email": "no-reply@greenhouse.io",
        "subject": "Your application",
        "sent_at": datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC),
        "body_text": "Thanks for applying.",
    }
    base.update(kw)
    return ImportedMessage(**base)


def _row(uid: int, mid: str = "<m1@x>"):
    return db.query_one(
        "SELECT * FROM email_messages WHERE user_id = %s AND provider_message_id = %s",
        (uid, mid),
    )


def test_stores_and_records_the_prefilter_verdict(f):
    uid = f.make_user()
    assert mail_store.store_messages(uid, [_msg()]) == 1
    row = _row(uid)
    assert row is not None
    assert row["subject"] == "Your application"
    # Recorded as imported, not recomputed later against rules that may have
    # changed by then.
    assert row["prefilter_hit"] is True
    assert row["prefilter_reason"].startswith("ats_domain:")


def test_the_same_message_twice_is_one_row(f):
    uid = f.make_user()
    mail_store.store_messages(uid, [_msg()])
    mail_store.store_messages(uid, [_msg(source="olm")])
    n = db.query_one("SELECT COUNT(*) AS c FROM email_messages WHERE user_id = %s", (uid,))
    assert n is not None and n["c"] == 1


def test_a_thinner_copy_never_blanks_a_filled_field(f):
    """An .olm export can lack a body the Gmail copy has. Re-importing must
    only ever improve a row - overwriting a body with NULL would silently
    destroy the thing the classifier reads."""
    uid = f.make_user()
    mail_store.store_messages(uid, [_msg()])
    mail_store.store_messages(uid, [_msg(source="olm", body_text=None, subject=None)])
    row = _row(uid)
    assert row is not None
    assert row["body_text"] == "Thanks for applying."
    assert row["subject"] == "Your application"


def test_a_richer_copy_fills_a_gap(f):
    """The reverse: an archive with no body, then a source that has one."""
    uid = f.make_user()
    mail_store.store_messages(uid, [_msg(body_text=None)])
    mail_store.store_messages(uid, [_msg(body_text="Now we have the text.")])
    row = _row(uid)
    assert row is not None
    assert row["body_text"] == "Now we have the text."


def test_two_users_may_hold_the_same_message(f):
    """Dedupe is per user. The same ATS mail reaching two accounts is two
    people's evidence, not a duplicate."""
    a, b = f.make_user(), f.make_user()
    mail_store.store_messages(a, [_msg()])
    mail_store.store_messages(b, [_msg()])
    assert _row(a) is not None
    assert _row(b) is not None


def test_batches_larger_than_one_chunk(f):
    """38,685 messages go in as ~39 statements, not 38,685 round trips."""
    uid = f.make_user()
    many = [_msg(provider_message_id=f"<bulk-{i}@x>") for i in range(mail_store.INSERT_CHUNK + 25)]
    assert mail_store.store_messages(uid, many) == mail_store.INSERT_CHUNK + 25
    n = db.query_one("SELECT COUNT(*) AS c FROM email_messages WHERE user_id = %s", (uid,))
    assert n is not None and n["c"] == mail_store.INSERT_CHUNK + 25


def test_ordinary_mail_is_stored_too(f):
    """The prefilter gates nothing. A bank statement is imported, marked as
    not job-related, and still classified - because a filtered-out email is
    the one unrecoverable failure in this pipeline."""
    uid = f.make_user()
    mail_store.store_messages(
        uid,
        [
            _msg(
                provider_message_id="<bank@x>",
                from_email="alerts@chase.com",
                subject="Statement ready",
                body_text="Your statement is available.",
            )
        ],
    )
    row = _row(uid, "<bank@x>")
    assert row is not None
    assert row["prefilter_hit"] is False
    assert row["prefilter_reason"] == "no_signal"
