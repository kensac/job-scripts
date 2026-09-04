"""Matching an email to an application, and refusing to guess.

A wrong match is worse than no match: everything downstream treats a match as
fact, so a confident wrong answer is unfalsifiable. These tests are mostly
about the cases where the right behaviour is to return nothing.
"""

from __future__ import annotations

import datetime

from api import db, mail_match


def _application(user_id: int, *, company=None, title=None, job_id=None, applied_at=None) -> int:
    row = db.query_one(
        """
        INSERT INTO applications (user_id, job_id, company_name, title, applied_at)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (user_id, job_id, company, title, applied_at),
    )
    assert row is not None
    return row["id"]


def test_exact_link_beats_everything(f):
    """ATS mail links back to the posting, and core.ats reduces both sides to
    the same canonical form - so this tier agrees by construction rather than
    by a parallel spelling."""
    uid = f.make_user()
    job_id = f.make_job(url="https://job-boards.greenhouse.io/acme/jobs/4041")
    app = _application(uid, company="Acme", title="Engineer", job_id=job_id)
    match = mail_match.match_message(
        uid,
        body="Update: https://job-boards.greenhouse.io/acme/jobs/4041?utm_source=x",
        company=None,
        title=None,
    )
    assert match.application_id == app
    assert match.method == mail_match.EXACT_LINK
    assert match.confidence == "high"


def test_company_name_variants_agree(f):
    """'Stripe' and 'Stripe, Inc.' are the same employer. Exact equality would
    treat them as different - the weakness that makes the reposted-role count
    an undercount elsewhere in this codebase."""
    uid = f.make_user()
    app = _application(uid, company="Stripe, Inc.", title="Backend Engineer")
    match = mail_match.match_message(uid, body="", company="Stripe", title=None)
    assert match.application_id == app
    assert match.method == mail_match.ATS_COMPANY


def test_two_applications_at_one_company_is_not_guessed(f):
    """The case a human or a model should settle. Guessing produces a
    confident wrong answer that nothing downstream can question."""
    uid = f.make_user()
    _application(uid, company="Acme", title="Backend Engineer")
    _application(uid, company="Acme", title="Frontend Engineer")
    match = mail_match.match_message(uid, body="", company="Acme", title=None)
    assert match.application_id is None
    assert match.method == mail_match.UNMATCHED


def test_title_disambiguates_where_company_alone_cannot(f):
    uid = f.make_user()
    _application(uid, company="Acme", title="Backend Engineer")
    wanted = _application(uid, company="Acme", title="Frontend Engineer")
    match = mail_match.match_message(uid, body="", company="Acme", title="Frontend Engineer")
    assert match.application_id == wanted
    assert match.method == mail_match.COMPANY_TITLE


def test_no_candidate_is_a_recorded_outcome_not_a_gap(f):
    """UNMATCHED with a NULL application_id means "we looked and found
    nothing", which differs from never having looked - and is what lets a
    later re-run improve on it."""
    uid = f.make_user()
    match = mail_match.match_message(uid, body="", company="Nobody", title=None)
    assert match.application_id is None
    assert match.method == mail_match.UNMATCHED
    assert match.rationale


def test_an_application_with_no_job_still_matches(f):
    """The 2022 Outlook case. The posting was never in the catalog and never
    will be, and the application is still real."""
    uid = f.make_user()
    app = _application(uid, company="OldCo", title="Intern", job_id=None)
    match = mail_match.match_message(uid, body="", company="OldCo", title=None)
    assert match.application_id == app


def test_a_message_predating_the_application_does_not_match(f):
    """Mail sent before you applied is not about that application."""
    uid = f.make_user()
    _application(
        uid,
        company="Acme",
        title="Engineer",
        applied_at=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
    )
    match = mail_match.match_message(
        uid,
        body="",
        company="Acme",
        title=None,
        sent_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    assert match.application_id is None


def test_matches_are_append_only(f):
    """Matching re-runs as new board rows appear, so a message unmatchable in
    March can match in September. A column on the message would destroy that;
    the newest row wins, as with the verdict log."""
    uid = f.make_user()
    f.make_job(url="https://x.test/m1")
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source) "
        "VALUES (%s, %s, %s) RETURNING id",
        (uid, "<match1@x>", "gmail"),
    )
    assert row is not None
    mid = row["id"]
    app = _application(uid, company="Acme", title="Engineer")

    mail_match.record(mid, mail_match.Match(None, mail_match.UNMATCHED, "none", "nothing yet"))
    assert mail_match.latest(mid)["application_id"] is None

    mail_match.record(mid, mail_match.Match(app, mail_match.ATS_COMPANY, "medium", "later"))
    current = mail_match.latest(mid)
    assert current["application_id"] == app
    # The earlier non-match is still there: it is evidence about when we knew
    # what, not a mistake to erase.
    n = db.query_one("SELECT COUNT(*) AS c FROM application_matches WHERE message_id = %s", (mid,))
    assert n is not None and n["c"] == 2


def test_canonical_urls_ignores_non_ats_links(f):
    """A signature link or a tracking pixel must not become a match."""
    found = mail_match.canonical_urls(
        "see https://example.com/blog and https://job-boards.greenhouse.io/acme/jobs/7"
    )
    assert any("greenhouse" in u for u in found)
    assert not any("example.com" in u for u in found)


def test_an_internship_rejection_does_not_attach_to_a_full_time_application(f):
    """The company tier matched on employer alone and never consulted the role
    it already had, so an internship rejection attached itself to a full-time
    application whenever that was the only one on file - a Data Science
    internship onto "Software Development Engineer I"."""
    uid = f.make_user()
    _application(
        uid,
        company="Acme",
        title="Software Development Engineer I",
        applied_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    match = mail_match.match_message(
        uid,
        body=None,
        company="Acme",
        title="Data Science Intern",
        sent_at=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
    )
    assert match.application_id is None
    assert match.method == mail_match.UNMATCHED


def test_the_veto_is_silent_when_either_title_is_missing(f):
    """A veto, not a requirement. 330 of this tier's matches have no title on
    one side, and those were never the problem."""
    uid = f.make_user()
    app = _application(
        uid,
        company="Acme",
        title="Software Development Engineer I",
        applied_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    match = mail_match.match_message(
        uid,
        body=None,
        company="Acme",
        title=None,
        sent_at=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
    )
    assert match.application_id == app


def test_two_internships_still_match_each_other(f):
    """It tests a categorical contradiction, not similarity. Both sides being
    internships is agreement, however differently they are worded."""
    uid = f.make_user()
    app = _application(
        uid,
        company="Acme",
        title="Software Engineering Intern, Summer 2026",
        applied_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    match = mail_match.match_message(
        uid,
        body=None,
        company="Acme",
        title="SWE Internship (Backend)",
        sent_at=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
    )
    assert match.application_id == app
