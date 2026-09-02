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
