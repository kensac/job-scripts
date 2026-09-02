"""The sender signal: is this an employer relationship at all?

Three organisations were caught by three different special cases and a fourth
would have needed a fifth. This is the general shape instead - and it is a
possibility, never a verdict, because the failure it catches looks exactly like
a real application submitted through a job board.
"""

from __future__ import annotations

import datetime

from api import db, mail_pipeline
from core import ats


def _app(f, uid: int, company: str, domain: str, day: int) -> int:
    row = db.query_one(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, %s, 'email') RETURNING id",
        (uid, company),
    )
    assert row is not None
    msg = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
        "subject, sent_at) VALUES (%s, %s, 'gmail', %s, 'hi', %s) RETURNING id",
        (
            uid,
            f"<{company}-{day}@x>",
            f"jobs@{domain}",
            datetime.datetime(2026, 1, day, tzinfo=datetime.UTC),
        ),
    )
    assert msg is not None
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s, %s, 'company_name', 'high')",
        (msg["id"], row["id"]),
    )
    return row["id"]


# --- the ATS half --------------------------------------------------------


def test_ats_mail_domains_are_recognised_including_the_ones_that_differ():
    """Greenhouse posts on greenhouse.io and MAILS from greenhouse-mail.io;
    Workday posts on myworkdayjobs.com and mails from myworkday.com. Deriving
    the mail side from the posting markers would miss the two providers that
    account for the most applications."""
    for domain in (
        "myworkday.com",
        "us.greenhouse-mail.io",
        "hire.lever.co",
        "talent.icims.com",
        "ashbyhq.com",
        "smartrecruiters.com",
    ):
        assert ats.is_ats_email_domain(domain) is True, domain


def test_a_lookalike_domain_is_not_an_ats():
    """Suffix matching must be on a dot boundary, or anyone can register
    greenhouse.io.example.com and inherit near-proof of a real application."""
    assert ats.is_ats_email_domain("notgreenhouse.io.evil.com") is False
    assert ats.is_ats_email_domain("greenhouse.io.evil.com") is False
    assert ats.is_ats_email_domain("psu.edu") is False
    assert ats.is_ats_email_domain(None) is False
    assert ats.is_ats_email_domain("") is False


# --- the spread half -----------------------------------------------------


def test_a_domain_serving_many_companies_is_flagged_for_review(f):
    """psu.edu was the first sender for 33 different company names across 103
    applications. One employer's own mail server sends about one company."""
    uid = f.make_user()
    for i, company in enumerate(("Programme A", "Programme B", "Programme C"), start=1):
        _app(f, uid, company, "university.test", i)

    signal = mail_pipeline.sender_signal(uid)
    assert len(signal) == 3
    for value in signal.values():
        assert value["review_suggested"] is True
        assert value["sender_company_count"] == 3
        assert "3 different companies" in value["why"]


def test_an_employers_own_domain_is_not_flagged(f):
    """Epic Games, MathWorks, Lockheed Martin and Citadel all mail from their
    own domains and are all real. Flagging non-ATS senders would have discarded
    5.7 real applications per junk one."""
    uid = f.make_user()
    for day in (1, 2, 3, 4):
        _app(f, uid, "Epic Games", "epicgames.test", day)

    signal = mail_pipeline.sender_signal(uid)
    assert len(signal) == 4
    for value in signal.values():
        assert value["review_suggested"] is False
        assert value["sender_is_ats"] is False
        assert value["sender_company_count"] == 1


def test_an_ats_serving_many_companies_is_never_flagged(f):
    """An ATS is an intermediary BY DESIGN and serves hundreds of employers, so
    the spread rule would flag every real application if the ATS check did not
    come first. This is the case that decides the order of the two rules."""
    uid = f.make_user()
    for i, company in enumerate(("Acme", "Globex", "Initech", "Umbrella"), start=1):
        _app(f, uid, company, "us.greenhouse-mail.io", i)

    signal = mail_pipeline.sender_signal(uid)
    for value in signal.values():
        assert value["sender_is_ats"] is True
        assert value["review_suggested"] is False, "an ATS is an intermediary on purpose"
        assert value["sender_company_count"] == 4


def test_two_companies_is_below_the_threshold(f):
    """The junk rate is 4% at one company name and 9% at two - the base rate -
    then 41% at three. The threshold sits where the data jumps, not where it
    looked tidy."""
    uid = f.make_user()
    _app(f, uid, "Company One", "shared.test", 1)
    _app(f, uid, "Company Two", "shared.test", 2)

    signal = mail_pipeline.sender_signal(uid)
    assert all(v["review_suggested"] is False for v in signal.values())
    assert all(v["sender_company_count"] == 2 for v in signal.values())


def test_the_signal_is_scoped_to_one_user(f):
    """The spread is a fact about THIS user's mail. Another user's applications
    through the same domain must not push it over the threshold."""
    mine = f.make_user()
    theirs = f.make_user()
    _app(f, mine, "Only Mine", "shared.test", 1)
    for i, company in enumerate(("A", "B", "C", "D"), start=2):
        _app(f, theirs, company, "shared.test", i)

    signal = mail_pipeline.sender_signal(mine)
    assert len(signal) == 1
    only = next(iter(signal.values()))
    assert only["sender_company_count"] == 1
    assert only["review_suggested"] is False


def test_it_recomputes_rather_than_freezing_at_match_time(f):
    """A domain that becomes an intermediary AFTER a match was made has to
    change the answer for matches already written. That is why this is read
    time and not a column."""
    uid = f.make_user()
    first = _app(f, uid, "Looks Legit", "later.test", 1)
    assert mail_pipeline.sender_signal(uid)[first]["review_suggested"] is False

    _app(f, uid, "Second Name", "later.test", 2)
    _app(f, uid, "Third Name", "later.test", 3)

    assert mail_pipeline.sender_signal(uid)[first]["review_suggested"] is True
