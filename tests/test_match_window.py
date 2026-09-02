"""The window a message must fall inside to match an application.

The asymmetry here is the whole point: a tracker date is a DAY and admits the
evening before it, while a message a week earlier belongs to a different,
earlier application and must stay out.
"""

from __future__ import annotations

import datetime

from api import db, mail_match


def _app(uid: int, company: str, provenance: str, applied_at: datetime.datetime) -> int:
    row = db.query_one(
        "INSERT INTO applications (user_id, company_name, source_provenance, applied_at) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (uid, company, provenance, applied_at),
    )
    assert row is not None
    return row["id"]


MIDNIGHT = datetime.datetime(2026, 8, 9, 0, 0, tzinfo=datetime.UTC)


def test_a_same_evening_acknowledgement_matches_a_tracker_application(f):
    """The real failure: an ATS acknowledged at 23:57 UTC - 19:57 in New York,
    the same summer evening he applied - and a midnight that exists only
    because a `date` was cast to an instant put it out of range."""
    uid = f.make_user()
    _app(uid, "Acme", "tracker", MIDNIGHT)

    sent = datetime.datetime(2026, 8, 8, 23, 57, tzinfo=datetime.UTC)
    match = mail_match._by_company(uid, "Acme", sent)
    assert match is not None, "a same-evening acknowledgement belongs to that application"


def test_the_slack_is_one_day_and_not_a_week(f):
    """469 days is not one day. The genuinely-earlier applications - mail from
    a previous cycle at a company applied to again later - must stay out, or
    this fix would swallow the thing it sits next to."""
    uid = f.make_user()
    _app(uid, "Acme", "tracker", MIDNIGHT)

    assert mail_match._by_company(uid, "Acme", MIDNIGHT - datetime.timedelta(days=7)) is None
    assert mail_match._by_company(uid, "Acme", MIDNIGHT - datetime.timedelta(days=469)) is None


def test_the_boundary_sits_exactly_one_day_back(f):
    uid = f.make_user()
    _app(uid, "Acme", "tracker", MIDNIGHT)

    assert mail_match._by_company(uid, "Acme", MIDNIGHT - datetime.timedelta(days=1)) is not None
    assert (
        mail_match._by_company(uid, "Acme", MIDNIGHT - datetime.timedelta(days=1, seconds=1))
        is None
    )


def test_an_email_derived_application_gets_no_slack(f):
    """Its applied_at is a real timestamp read off a message, not a date cast
    to midnight, so it carries none of that uncertainty. Widening it here
    would hide a different bug rather than fix one."""
    uid = f.make_user()
    _app(uid, "Acme", "email", MIDNIGHT)

    assert mail_match._by_company(uid, "Acme", MIDNIGHT) is not None
    assert mail_match._by_company(uid, "Acme", MIDNIGHT - datetime.timedelta(seconds=1)) is None


def test_ambiguity_still_refuses_to_choose(f):
    """Two applications at one employer is the case a human settles. The slack
    must not turn a refusal into a guess."""
    uid = f.make_user()
    _app(uid, "Acme", "tracker", MIDNIGHT)
    _app(uid, "Acme", "tracker", MIDNIGHT - datetime.timedelta(days=200))

    assert mail_match._by_company(uid, "Acme", MIDNIGHT) is None
