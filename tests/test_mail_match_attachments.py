from __future__ import annotations

import datetime

from api import db
from api.board.person_state import write_board_row
from tasks.mail.matching import handler as task
from tests import mail_log
from tests.mail_match_support import _application, _event, _message


def test_a_recruiter_approach_is_never_attached_to_an_application(f):
    """RippleMatch is a job platform AND a company Kanishk applied to. Its
    marketing mail extracted company='RippleMatch', matched his real
    RippleMatch application by name, and 27 platform nudges became evidence
    about a job he actually applied for."""
    uid = f.make_user()
    job = f.make_job(company="RippleMatch", title="Software Engineer")
    write_board_row(
        uid, job, {"status": "Application Submitted", "date_applied": None}, publish=False
    )

    msg = _message(uid, subject="still looking for new roles?")
    _event(msg, "recruiter_outreach", company="RippleMatch", title="Software Engineer")

    counts = task.match_pending(uid)
    assert counts == {"not_an_application": 1}
    row = db.query_one(
        "SELECT application_id, method FROM application_matches WHERE message_id = %s", (msg,)
    )
    assert row["application_id"] is None
    assert row["method"] == "not_an_application", (
        "deliberately unattached is not the same as 'we looked and found nothing'"
    )


def test_an_approach_already_attached_is_corrected(f):
    """Matching is append-only and match_pending only considers messages with
    no current match, so a rule change never reaches what the old rule decided.
    Without this the fix applies to new mail while the existing wrong
    attachments stay - a corrected matcher still reporting the numbers that
    prompted the correction."""
    uid = f.make_user()
    app = db.query_one(
        "INSERT INTO applications (user_id, company_name, title, source_provenance) "
        "VALUES (%s,'RippleMatch','Software Engineer','tracker') RETURNING id",
        (uid,),
    )["id"]
    msg = _message(uid)
    _event(msg, "recruiter_outreach", company="RippleMatch", title="Software Engineer")
    mail_log.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s,%s,'ats_company','medium')",
        (msg, app),
    )

    assert task.detach_unattachable(uid) == 1
    assert task.detach_unattachable(uid) == 0, "idempotent"
    row = db.query_one(
        "SELECT application_id FROM application_matches WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (msg,),
    )
    assert row["application_id"] is None


def test_employer_mail_relayed_by_a_platform_still_matches(f):
    """The matches through RippleMatch to real employers were CORRECT - it
    relays employer decisions and the classifier reads the employer. Only the
    platform's own mail was wrong, so the fix must not cost the rest."""
    uid = f.make_user()
    job = f.make_job(company="Plaid", title="Software Engineer")
    write_board_row(
        uid, job, {"status": "Application Submitted", "date_applied": None}, publish=False
    )

    msg = _message(uid, subject="An update from Plaid")
    _event(msg, "rejection", company="Plaid", title="Software Engineer")

    counts = task.match_pending(uid)
    assert counts == {"ats_company": 1}


def test_titleless_evidence_still_leaves_the_message_unmatched_here(f):
    """seed_from_mail only creates applications; attaching is the matcher's
    job. Lowering the floor is what lets the tiers reach the message on the
    next run, which is the re-runnable path rather than a second matcher."""
    uid = f.make_user()
    early = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    bare = _message(uid, sent_at=early)
    _event(bare, "acknowledgement", company="Battelle")
    titled = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(titled, "acknowledgement", company="Battelle", title="Software Engineer")

    _created, matched = task.seed_from_mail(uid)
    assert matched == 1, "only the titled group's own messages are attached here"


def test_an_unmatched_message_is_reconsidered_when_applications_appear(f):
    """The module's own claim: a message that could not be matched in March
    can match in September. It could not - once `unmatched` was written the
    message was frozen against whatever applications existed at that moment,
    and 8,462 verdicts were written before 1,779 applications were created."""
    uid = f.make_user()
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(mid, "rejection", company="Acme", title="Backend Engineer")

    task.match_pending(uid)
    assert (
        db.query_one(
            "SELECT application_id FROM application_matches WHERE message_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (mid,),
        )["application_id"]
        is None
    )

    app = _application(
        uid,
        company="Acme",
        title="Backend Engineer",
        applied_at=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
    )
    task.match_pending(uid)
    assert (
        db.query_one(
            "SELECT application_id FROM application_matches WHERE message_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (mid,),
        )["application_id"]
        == app
    )


def test_a_sweep_that_changes_nothing_writes_nothing(f):
    """Re-deciding every unmatched message each cycle would append a fresh
    'still nothing' row per message per sweep - at 6,130 unmatched and an
    hourly cycle, ~147,000 rows a day of a log repeating itself, burying the
    transitions it exists to show."""
    uid = f.make_user()
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(mid, "rejection", company="Nowhere", title="Role")

    task.match_pending(uid)
    after_first = db.query_one(
        "SELECT count(*) AS n FROM application_matches WHERE message_id = %s", (mid,)
    )["n"]
    for _ in range(3):
        task.match_pending(uid)
    after_more = db.query_one(
        "SELECT count(*) AS n FROM application_matches WHERE message_id = %s", (mid,)
    )["n"]
    assert after_first == 1
    assert after_more == 1, "an unchanged verdict must not be logged again"


def test_a_real_transition_is_still_appended(f):
    """Suppressing repeats must not suppress changes - the log is what makes a
    re-run measurable against the one before it."""
    uid = f.make_user()
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(mid, "rejection", company="Acme", title="Backend Engineer")
    task.match_pending(uid)

    _application(
        uid,
        company="Acme",
        title="Backend Engineer",
        applied_at=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
    )
    task.match_pending(uid)
    rows = db.query(
        "SELECT application_id, method FROM application_matches WHERE message_id = %s ORDER BY id",
        (mid,),
    )
    assert len(rows) == 2, "the transition from unmatched to matched is a real entry"
    assert rows[0]["application_id"] is None and rows[1]["application_id"] is not None


def test_a_human_verdict_is_recorded_even_when_it_repeats_the_matcher(f):
    """Suppressing repeats applies to the MATCHER. That a person looked and
    affirmed the answer is a different fact from the tiers producing it again,
    and it is the fact actor_user_id exists to carry."""
    from api.mail import match as mail_match

    uid = f.make_user()
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(mid, "rejection", company="Nowhere", title="Role")
    task.match_pending(uid)

    standing = mail_match.latest(mid)
    mail_match.record(
        mid,
        mail_match.MatchDecision(
            standing.application_id, standing.method, standing.confidence, "affirmed"
        ),
        actor_user_id=uid,
    )
    rows = db.query(
        "SELECT actor_user_id FROM application_matches WHERE message_id = %s ORDER BY id", (mid,)
    )
    assert len(rows) == 2
    assert rows[0]["actor_user_id"] is None and rows[1]["actor_user_id"] == uid


def test_an_unattachable_kind_stops_reading_as_a_matching_failure(f):
    """1,307 recruiter approaches carried a stale `unmatched` from before they
    were reclassified, so they sat in the user's "say where this belongs"
    queue - work the system already knew belonged nowhere. Re-deciding them
    records the refusal the predicate downstream actually tests for."""
    from api.mail import match as mail_match

    uid = f.make_user()
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(mid, "rejection", company="Acme", title="Role")
    task.match_pending(uid)
    assert mail_match.latest(mid).method == "unmatched"

    _event(mid, "recruiter_outreach", company="Acme")
    task.match_pending(uid)
    assert mail_match.latest(mid).method == mail_match.NOT_AN_APPLICATION
