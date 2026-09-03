"""Identical input must not get different labels.

A regression metric with no ground truth in it: it asks only whether the
classifier agrees with itself, so it survives the corpus changing and the model
changing, and nobody has to label anything for it to keep working.
"""

from __future__ import annotations

import datetime

import pytest

from api import db
from tests.conftest import _auth_headers

BODY = "Thank you for applying. " * 20


def _msg(uid: int, mid: str, body: str, kind: str, sender: str = "hr@acme.test") -> int:
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, subject, "
        "sent_at, body_text) VALUES (%s, %s, 'gmail', %s, 'Your application', %s, %s) RETURNING id",
        (uid, mid, sender, datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC), body),
    )
    assert row is not None
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, model) "
        "VALUES (%s, %s, 'high', 'gpt-5-nano')",
        (row["id"], kind),
    )
    return row["id"]


@pytest.fixture
def admin(client):
    headers = _auth_headers("cons-admin", "cons@example.com", ["infra-admins"])
    assert client.post("/v1/users/bootstrap", headers=headers).status_code == 200
    return headers


def test_identical_bodies_with_one_kind_are_consistent(client, admin, f):
    uid = f.make_user()
    for i in range(3):
        _msg(uid, f"<same{i}@x>", BODY, "acknowledgement")

    body = client.get("/v1/admin/mail/consistency", headers=admin).json()
    assert body["groups"]["numerator"] == 0
    assert body["worst"] == []


def test_identical_bodies_with_different_kinds_are_caught(client, admin, f):
    """The real case: 210 copies of one HackPSU RSVP body came back as both
    not_job_related and offer."""
    uid = f.make_user()
    _msg(uid, "<a@x>", BODY, "acknowledgement")
    _msg(uid, "<b@x>", BODY, "acknowledgement")
    _msg(uid, "<c@x>", BODY, "rejection")

    body = client.get("/v1/admin/mail/consistency", headers=admin).json()
    assert body["groups"]["numerator"] == 1
    worst = body["worst"][0]
    assert worst["copies"] == 3
    assert worst["kinds"] == ["acknowledgement", "rejection"]
    assert worst["example_message_id"] is not None


def test_a_reference_number_does_not_split_a_template(client, admin, f):
    """A template differing only by a requisition or ticket number is the same
    input. Without normalising digits, every copy hashes apart and the metric
    silently measures nothing."""
    uid = f.make_user()
    for i in range(3):
        _msg(uid, f"<ref{i}@x>", BODY + f" Reference {1000 + i}.", "acknowledgement")

    body = client.get("/v1/admin/mail/consistency", headers=admin).json()
    assert body["coverage"]["messages_covered"] == 3, "one group, not three"


def test_different_senders_are_different_templates(client, admin, f):
    """The same words from two companies are two templates, and agreeing that
    they are one would manufacture disagreements out of unrelated mail."""
    uid = f.make_user()
    _msg(uid, "<s1@x>", BODY, "acknowledgement", sender="hr@one.test")
    _msg(uid, "<s2@x>", BODY, "acknowledgement", sender="hr@one.test")
    _msg(uid, "<s3@x>", BODY, "rejection", sender="hr@two.test")
    _msg(uid, "<s4@x>", BODY, "rejection", sender="hr@two.test")

    body = client.get("/v1/admin/mail/consistency", headers=admin).json()
    assert body["groups"]["numerator"] == 0, "two consistent templates, not one split one"


def test_the_rate_travels_with_what_it_is_a_rate_of(client, admin, f):
    """Only repeated bodies are checkable this way, so it is a canary over part
    of the corpus. Quoted without its denominator it would read as an accuracy
    figure for the whole thing."""
    uid = f.make_user()
    for i in range(3):
        _msg(uid, f"<cov{i}@x>", BODY, "acknowledgement")
    _msg(uid, "<lonely@x>", "a completely different body " * 20, "rejection")

    body = client.get("/v1/admin/mail/consistency", headers=admin).json()
    assert body["coverage"]["messages_covered"] == 3
    assert body["coverage"]["messages_classified"] == 4
    assert body["coverage"]["min_copies"] == 2


def test_a_stub_body_is_not_a_template(client, admin, f):
    """Thousands of bare signatures and one-line auto-replies hash together
    into a group that means nothing."""
    uid = f.make_user()
    _msg(uid, "<t1@x>", "thanks", "acknowledgement")
    _msg(uid, "<t2@x>", "thanks", "rejection")

    body = client.get("/v1/admin/mail/consistency", headers=admin).json()
    assert body["coverage"]["messages_covered"] == 0


def test_an_ordinary_user_cannot_read_it(client, f):
    headers = _auth_headers("cons-plain", "consplain@example.com", ["jobtracker-users-internal"])
    assert client.post("/v1/users/bootstrap", headers=headers).status_code == 200
    assert client.get("/v1/admin/mail/consistency", headers=headers).status_code == 403
