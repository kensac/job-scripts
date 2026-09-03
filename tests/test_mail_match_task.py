"""Populating applications, which is what the matcher was always missing.

The matcher had no defect; `applications` was empty, so every tier correctly
returned nothing and the outcome side of the product stayed dark. These tests
are about what gets created, what deliberately does not, and the ordering that
keeps the matcher able to answer at all.
"""

from __future__ import annotations

import datetime
import itertools

import pytest

from api import db
from api.tasks import mail_match as task

_seq = itertools.count(1)


def _message(user_id: int, *, subject="s", body="b", sent_at=None, thread=None) -> int:
    row = db.query_one(
        """
        INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id,
                                    source, subject, body_text, sent_at)
        VALUES (%s, %s, %s, 'takeout', %s, %s, %s) RETURNING id
        """,
        (user_id, f"msg-{next(_seq)}", thread, subject, body, sent_at),
    )
    assert row is not None
    return row["id"]


def _application(user_id: int, *, company=None, title=None, applied_at=None) -> int:
    row = db.query_one(
        """
        INSERT INTO applications (user_id, job_id, company_name, title, applied_at)
        VALUES (%s, NULL, %s, %s, %s) RETURNING id
        """,
        (user_id, company, title, applied_at),
    )
    assert row is not None
    return row["id"]


def _event(message_id: int, kind: str, *, company=None, title=None) -> int:
    detail = {}
    if company:
        detail["company"] = company
    if title:
        detail["role_title"] = title
    row = db.query_one(
        "INSERT INTO email_events (message_id, kind, confidence, detail) "
        "VALUES (%s, %s, 'high', %s) RETURNING id",
        (message_id, kind, db.jsonb(detail)),
    )
    assert row is not None
    return row["id"]


def test_tracker_applications_are_seeded_once(f):
    uid = f.make_user()
    job = f.make_job(company="Acme", title="Engineer")
    f.make_board_row(uid, job, status="Application Submitted")

    assert task.seed_from_tracker(uid) == 1
    assert task.seed_from_tracker(uid) == 0, "re-running must not duplicate"

    row = db.query_one("SELECT company_name, job_id, source_provenance FROM applications")
    assert row is not None
    assert row["company_name"] == "Acme"
    assert row["job_id"] == job
    assert row["source_provenance"] == "tracker"


def test_a_posting_the_user_declined_is_not_an_application(f):
    """634 of this user's board rows are 'No Longer Interested'. Seeding those
    would invent a job search that did not happen."""
    uid = f.make_user()
    job = f.make_job(company="Acme")
    f.make_board_row(uid, job, status="No Longer Interested")
    assert task.seed_from_tracker(uid) == 0


def test_mail_creates_the_application_a_dead_posting_left_behind(f):
    """The 2022 case: the posting was never in this catalog and never will be.
    The rejection still carries who and what, and that is the backfill's value."""
    uid = f.make_user()
    msg = _message(uid, sent_at=datetime.datetime(2022, 6, 1, tzinfo=datetime.UTC))
    _event(msg, "rejection", company="Initech", title="Backend Intern")

    task.match_pending(uid)
    created, matched = task.seed_from_mail(uid)

    assert (created, matched) == (1, 1)
    row = db.query_one("SELECT job_id, company_name, source_provenance FROM applications")
    assert row is not None
    assert row["job_id"] is None, "never synthesise a jobs row from an email"
    assert row["company_name"] == "Initech"
    assert row["source_provenance"] == "email"


def test_recruiter_outreach_does_not_create_an_application(f):
    """An approach is not an application. This is the one kind that routinely
    arrives from companies the user has no relationship with."""
    uid = f.make_user()
    msg = _message(uid)
    _event(msg, "recruiter_outreach", company="Globex", title="Staff Engineer")

    task.match_pending(uid)
    created, _ = task.seed_from_mail(uid)
    assert created == 0


def test_mail_does_not_add_a_second_application_at_a_tracked_company(f):
    """The poisoning case. `_by_company` refuses to choose between two
    candidates, so manufacturing a rival at a company that already has one
    would make every future message there permanently unmatchable."""
    uid = f.make_user()
    job = f.make_job(company="Acme", title="Engineer")
    f.make_board_row(uid, job, status="Application Submitted")
    task.seed_from_tracker(uid)

    msg = _message(uid, sent_at=datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC))
    _event(msg, "rejection", company="Acme, Inc.", title="Something Else Entirely")

    created, _ = task.seed_from_mail(uid)
    assert created == 0
    assert db.query_one("SELECT count(*) AS n FROM applications")["n"] == 1


def test_one_application_per_thread_not_per_message(f):
    uid = f.make_user()
    for subject in ("Application received", "Update on your application"):
        msg = _message(uid, subject=subject, thread="thread-1")
        _event(msg, "acknowledgement", company="Umbrella", title="Analyst")

    task.match_pending(uid)
    created, matched = task.seed_from_mail(uid)
    assert created == 1
    assert matched == 2, "both messages attach to the one application"


def test_unmatched_is_recorded_rather_than_skipped(f):
    """'We looked and found nothing' is a different state from 'we never
    looked', and only the first one lets a later re-run be measured."""
    uid = f.make_user()
    msg = _message(uid)
    _event(msg, "rejection", company="Nobody", title="Nothing")

    counts = task.match_pending(uid)
    assert counts == {"unmatched": 1}
    row = db.query_one(
        "SELECT application_id, method FROM application_matches WHERE message_id = %s", (msg,)
    )
    assert row is not None
    assert row["application_id"] is None
    assert row["method"] == "unmatched"


def test_not_job_related_mail_is_never_matched(f):
    uid = f.make_user()
    msg = _message(uid)
    _event(msg, "not_job_related")
    assert task.match_pending(uid) == {}


def test_a_retracted_rejection_stops_creating_an_application(f):
    """Events are append-only and the newest wins. A message reclassified away
    from a rejection must stop counting as evidence that an application
    exists - the same retraction rule `events_for` implements."""
    uid = f.make_user()
    msg = _message(uid)
    _event(msg, "rejection", company="Hooli", title="Engineer")
    _event(msg, "not_job_related")

    task.match_pending(uid)
    created, _ = task.seed_from_mail(uid)
    assert created == 0


def test_mail_with_no_role_does_not_become_an_application(f):
    """An application is a (company, role) pair. Mail naming an employer but no
    role - a careers newsletter, an event announcement - is half an entity, and
    recording it would put a row in the funnel nothing can ever resolve."""
    uid = f.make_user()
    msg = _message(uid)
    _event(msg, "acknowledgement", company="Initech")
    task.match_pending(uid)
    created, _ = task.seed_from_mail(uid)
    assert created == 0


def test_an_organisation_the_user_helps_run_is_held_back(f):
    """A hackathon the user judges generates hundreds of genuine 'interview
    scheduled' messages where they are the interviewer. No event-kind rule
    separates those from applications - only the volume does."""
    uid = f.make_user()
    for i in range(task.CAP_FLOOR + 1):
        msg = _message(uid, subject=f"interview {i}")
        _event(msg, "interview_scheduled", company="HackPSU", title=f"Judge {i}")
    task.match_pending(uid)
    created, _ = task.seed_from_mail(uid)
    assert created == 0, "held for adjudication rather than invented"
    assert db.query_one("SELECT count(*) AS n FROM applications")["n"] == 0


def test_the_cap_rises_to_a_real_job_search(f):
    """This user really did apply to Tesla 43 times. Any fixed ceiling below
    that would delete real history, so the cap is read from their own tracker."""
    uid = f.make_user()
    for i in range(20):
        job = f.make_job(company="Tesla", title=f"Engineer {i}")
        f.make_board_row(uid, job, status="Application Submitted")
    task.seed_from_tracker(uid)
    assert task.derived_cap(uid) == 20

    other = f.make_user()
    assert task.derived_cap(other) == task.CAP_FLOOR, "a user with no history still derives"


@pytest.mark.asyncio
async def test_a_scheduled_run_covers_every_user(f):
    """The schedule is fleet-wide but matching is per-user by construction, so
    an unscoped run has to iterate. A task that silently did only the first
    user would look identical to one that did everybody."""
    first, second = f.make_user(), f.make_user()
    for uid in (first, second):
        job = f.make_job(company=f"Acme{uid}", title="Engineer")
        f.make_board_row(uid, job, status="Application Submitted")

    await task.handle_match_mail(f.make_task("match_mail", {}), {})

    for uid in (first, second):
        assert (
            db.query_one("SELECT count(*) AS n FROM applications WHERE user_id = %s", (uid,))["n"]
            == 1
        )


@pytest.mark.asyncio
async def test_matching_one_user_leaves_the_others_alone(f):
    first, second = f.make_user(), f.make_user()
    for uid in (first, second):
        job = f.make_job(company=f"Acme{uid}", title="Engineer")
        f.make_board_row(uid, job, status="Application Submitted")

    await task.handle_match_mail(f.make_task("match_mail", {}), {"user_id": first})

    assert (
        db.query_one("SELECT count(*) AS n FROM applications WHERE user_id = %s", (first,))["n"]
        == 1
    )
    assert (
        db.query_one("SELECT count(*) AS n FROM applications WHERE user_id = %s", (second,))["n"]
        == 0
    )


def test_a_recruiter_approach_is_never_attached_to_an_application(f):
    """RippleMatch is a job platform AND a company Kanishk applied to. Its
    marketing mail extracted company='RippleMatch', matched his real
    RippleMatch application by name, and 27 platform nudges became evidence
    about a job he actually applied for."""
    uid = f.make_user()
    job = f.make_job(company="RippleMatch", title="Software Engineer")
    f.make_board_row(uid, job, status="Application Submitted")
    task.seed_from_tracker(uid)

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
    db.execute(
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
    f.make_board_row(uid, job, status="Application Submitted")
    task.seed_from_tracker(uid)

    msg = _message(uid, subject="An update from Plaid")
    _event(msg, "rejection", company="Plaid", title="Software Engineer")

    counts = task.match_pending(uid)
    assert counts == {"ats_company": 1}


def test_a_second_worker_cannot_duplicate_a_derived_application(f):
    """Two workers can hold this task at once. A slow worker kept running this
    handler after the reaper had requeued its task and another worker had
    finished it - and nothing stopped the loser writing a second copy of every
    application the winner had just created.

    A duplicate here does not merely add a row: _by_company refuses to choose
    between two applications at one employer, so it would make every future
    message at that company permanently unmatchable, silently."""
    uid = f.make_user()
    msg = _message(uid)
    _event(msg, "rejection", company="Initech", title="Backend Intern")

    task.match_pending(uid)
    first_created, first_matched = task.seed_from_mail(uid)
    assert (first_created, first_matched) == (1, 1)

    # The losing worker: its own view of the world still says this message is
    # unmatched, because it read the rows before the winner wrote.
    second_created, _ = task.seed_from_mail(uid)
    assert second_created == 0
    assert db.query_one("SELECT count(*) AS n FROM applications")["n"] == 1


def test_a_shared_subject_does_not_collapse_distinct_employers(f):
    """The .olm importer filled provider_thread_id from ThreadTopic, a
    normalised subject. seed_from_mail prefers the thread key, so every ATS
    autoresponder sharing a subject grouped into ONE application and took its
    company from whichever message sorted first.

    "Nittany Lion Careers Application Confirmation" was 56 messages across 32
    employers, all attached to a single G3 Technologies application. This is
    that shape, minimised: same subject, different employers, and Outlook mail
    now arrives with no thread id so (company, title) decides.
    """
    uid = f.make_user()
    subject = "Nittany Lion Careers Application Confirmation"
    for company in ("Honor Device", "Modo Labs", "Manatal"):
        mid = _message(uid, subject=subject, thread=None)
        _event(mid, "acknowledgement", company=company, title="Software Engineer")

    created, _ = task.seed_from_mail(uid)
    assert created == 3, "one application per employer, not one per subject"
    companies = {
        r["company_name"]
        for r in db.query("SELECT company_name FROM applications WHERE user_id = %s", (uid,))
    }
    assert companies == {"Honor Device", "Modo Labs", "Manatal"}


def test_a_real_provider_thread_still_groups(f):
    """Takeout and Gmail thread ids are genuine threading identities - the
    first References entry and Gmail's own threadId - and neither has ever
    equalled a subject. Grouping by them stays."""
    uid = f.make_user()
    for _ in range(3):
        mid = _message(uid, subject="Re: your application", thread="<root@greenhouse.io>")
        _event(mid, "acknowledgement", company="Acme", title="Software Engineer")

    created, matched = task.seed_from_mail(uid)
    assert created == 1, "one real conversation is one application"
    assert matched == 3


def test_derived_applied_at_does_not_veto_its_own_earlier_evidence(f):
    """The floor was the earliest TITLED message, then used to rule out the
    title-less ones - 159 of 161 blocked messages carry no role_title, and
    Battelle missed by four seconds. The date was derived from a subset of the
    evidence and then used against the rest of it."""
    uid = f.make_user()
    early = datetime.datetime(2026, 3, 1, 9, 0, tzinfo=datetime.UTC)
    later = datetime.datetime(2026, 3, 1, 9, 0, 4, tzinfo=datetime.UTC)

    bare = _message(uid, sent_at=early)
    _event(bare, "acknowledgement", company="Battelle")
    titled = _message(uid, sent_at=later)
    _event(titled, "acknowledgement", company="Battelle", title="Software Engineer")

    created, _ = task.seed_from_mail(uid)
    assert created == 1
    row = db.query_one("SELECT applied_at FROM applications WHERE user_id = %s", (uid,))
    assert row["applied_at"] == early, "the floor must include the evidence it was derived from"


def test_titleless_evidence_is_not_guessed_at_an_ambiguous_company(f):
    """Two roles at one employer: a message naming neither cannot be assigned
    to one of them. Lowering both floors on its say-so would replace a silent
    wrong answer with a different silent wrong answer."""
    uid = f.make_user()
    early = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    later = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)

    bare = _message(uid, sent_at=early)
    _event(bare, "acknowledgement", company="Acme")
    for role in ("Backend Engineer", "Data Engineer"):
        mid = _message(uid, sent_at=later)
        _event(mid, "acknowledgement", company="Acme", title=role)

    created, _ = task.seed_from_mail(uid)
    assert created == 2
    dates = {
        r["applied_at"]
        for r in db.query("SELECT applied_at FROM applications WHERE user_id = %s", (uid,))
    }
    assert dates == {later}, "an ambiguous message must not lower either floor"


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
    from api import mail_match

    uid = f.make_user()
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(mid, "rejection", company="Nowhere", title="Role")
    task.match_pending(uid)

    standing = mail_match.latest(mid)
    mail_match.record(
        mid,
        mail_match.Match(
            standing["application_id"], standing["method"], standing["confidence"], "affirmed"
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
    from api import mail_match

    uid = f.make_user()
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(mid, "rejection", company="Acme", title="Role")
    task.match_pending(uid)
    assert mail_match.latest(mid)["method"] == "unmatched"

    _event(mid, "recruiter_outreach", company="Acme")
    task.match_pending(uid)
    assert mail_match.latest(mid)["method"] == mail_match.NOT_AN_APPLICATION
