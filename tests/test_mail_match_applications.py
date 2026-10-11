"""Application derivation from board state and existing mail events.

The matcher had no defect; `applications` was empty, so every tier correctly
returned nothing. These tests cover what creates an application, what does not,
and the ordering that keeps the matcher able to answer.
"""

from __future__ import annotations

import datetime

from api import db
from api.board.person_state import write_board_row
from tasks.mail.matching import handler as task
from tests.mail_match_support import _event, _message


def test_a_board_row_moving_into_an_applied_status_records_the_application(f):
    """Written with the status, not by a later sweep: the hourly
    seed_from_tracker this replaces was the only writer, so a status set
    between sweeps had no application for up to an hour."""
    uid = f.make_user()
    job = f.make_job(company="Acme", title="Engineer")
    write_board_row(uid, job, {"status": "Application Submitted"}, publish=False)
    write_board_row(uid, job, {"status": "Follow-up"}, publish=False)
    write_board_row(uid, job, {"notes": "called"}, publish=False)

    rows = db.query("SELECT company_name, job_id, source_provenance, applied_at FROM applications")
    assert len(rows) == 1, "re-writing the row must not duplicate"
    assert rows[0]["company_name"] == "Acme"
    assert rows[0]["job_id"] == job
    assert rows[0]["source_provenance"] == "tracker"
    assert rows[0]["applied_at"] is not None, "dated from the autofilled date_applied"


def test_a_posting_the_user_declined_is_not_an_application(f):
    """634 of this user's board rows are 'No Longer Interested'. Recording
    those would invent a job search that did not happen."""
    uid = f.make_user()
    job = f.make_job(company="Acme")
    write_board_row(uid, job, {"status": "No Longer Interested"}, publish=False)
    assert db.query("SELECT id FROM applications") == []


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
    write_board_row(
        uid, job, {"status": "Application Submitted", "date_applied": None}, publish=False
    )

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
        write_board_row(
            uid, job, {"status": "Application Submitted", "date_applied": None}, publish=False
        )
    assert task.derived_cap(uid) == 20

    other = f.make_user()
    assert task.derived_cap(other) == task.CAP_FLOOR, "a user with no history still derives"


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
