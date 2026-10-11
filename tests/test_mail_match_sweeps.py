from __future__ import annotations

import datetime

import pytest

from api import db
from api.board.person_state import write_board_row
from tasks import mail_match as task
from tests.mail_match_support import _application, _event, _message


@pytest.mark.asyncio
async def test_a_scheduled_run_covers_every_user(f):
    """The schedule is fleet-wide but matching is per-user by construction, so
    an unscoped run has to iterate. A task that silently did only the first
    user would look identical to one that did everybody."""
    first, second = f.make_user(), f.make_user()
    for uid in (first, second):
        _event(_message(uid), "rejection", company=f"Acme{uid}", title="Engineer")

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
        _event(_message(uid), "rejection", company=f"Acme{uid}", title="Engineer")

    await task.handle_match_mail(f.make_task("match_mail", {}), {"user_id": first})

    assert (
        db.query_one("SELECT count(*) AS n FROM applications WHERE user_id = %s", (first,))["n"]
        == 1
    )
    assert (
        db.query_one("SELECT count(*) AS n FROM applications WHERE user_id = %s", (second,))["n"]
        == 0
    )


@pytest.mark.asyncio
async def test_the_sweep_counter_does_not_report_itself_as_matches(f):
    """`swept` counts messages DECIDED; `attached` counts those that came out
    holding an application. They were one field called "matched", so a sweep
    that decided thousands and attached none read as thousands matched - and a
    sweep whose predicate selected nothing read as zero matched, which was
    taken as evidence a working change had recovered nothing."""
    uid = f.make_user()
    # Two messages that cannot attach: no application at either company, and
    # no role title, so seed_from_mail cannot derive one from them either.
    for i in range(2):
        mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
        _event(mid, "rejection", company=f"Nowhere{i}")

    await task.handle_match_mail(1, {"user_id": uid})
    label = db.query_one("SELECT progress->>'label' AS l FROM tasks WHERE id = 1")
    row = db.query_one(
        "SELECT count(*) AS n FROM application_matches am "
        "JOIN email_messages m ON m.id = am.message_id WHERE m.user_id = %s",
        (uid,),
    )
    assert row["n"] == 2, "both were decided"
    assert (
        db.query_one(
            "SELECT count(*) AS n FROM application_matches am "
            "JOIN email_messages m ON m.id = am.message_id "
            "WHERE m.user_id = %s AND am.application_id IS NOT NULL",
            (uid,),
        )["n"]
        == 0
    ), "and neither attached"
    if label and label["l"]:
        assert "2 messages swept" in label["l"]
        assert "0 now attached" in label["l"]


@pytest.mark.asyncio
async def test_one_users_sweep_advances_progress_as_it_goes(f, monkeypatch):
    """Progress written once per user made a whole run one step: production
    has one user, so a 110-minute sweep on a far host looked stalled to the
    detector for all of it. The task row must move inside the sweep, over
    messages, not only between users."""
    monkeypatch.setattr(task, "PROGRESS_EVERY", 2)
    uid = f.make_user()
    for i in range(3):
        mid = _message(uid)
        _event(mid, "rejection", company=f"Nowhere{i}")
    for i in range(3):
        job = f.make_job(company=f"Acme{i}", title="Engineer")
        write_board_row(
            uid, job, {"status": "Application Submitted", "date_applied": None}, publish=False
        )
    task_id = f.make_task("match_mail", {})

    written: list[str] = []
    original = task.set_progress

    def observe(*args, **kwargs):
        original(*args, **kwargs)
        row = db.query_one("SELECT progress->>'label' AS l FROM tasks WHERE id = %s", (task_id,))
        written.append(row["l"])

    monkeypatch.setattr(task, "set_progress", observe)
    await task.handle_match_mail(task_id, {"user_id": uid})

    assert [label for label in written if label.startswith(f"matching user {uid}:")] == [
        f"matching user {uid}: messages 0/3",
        f"matching user {uid}: messages 2/3",
    ]


def test_a_finished_sweep_is_not_repeated_when_nothing_changed(f):
    """`record` keeps an unchanged verdict's timestamp where it was, so any
    application newer than the verdict re-selected the message on every sweep:
    245 runs and 200.5 worker-hours in 14 days on production, 244 of them
    writing nothing. The last finished sweep's start is the cutoff now."""
    uid = f.make_user()
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(mid, "rejection", company="Nowhere", title="Role")
    task.match_pending(uid)
    _application(uid, company="Elsewhere", title="Other")

    # Without a cutoff the message is decided again every time.
    assert task.match_pending(uid) == {task.mail_match.UNMATCHED: 1}
    assert task.match_pending(uid) == {task.mail_match.UNMATCHED: 1}

    finished = f.make_task("match_mail", {}, status="done")
    db.execute("UPDATE tasks SET started_at = now() WHERE id = %s", (finished,))
    since = task.mail_match.last_sweep_start(uid)
    assert since is not None
    assert task.match_pending(uid, since=since) == {}
    assert not task.mail_match.changed_since(since, uid)
    assert not task.mail_match.changed_since(task.mail_match.last_sweep_start())

    # A new candidate is a reason to look again.
    _application(uid, company="Another", title="Role")
    assert task.mail_match.changed_since(since, uid)
    assert task.mail_match.changed_since(task.mail_match.last_sweep_start())
    assert task.match_pending(uid, since=since) == {task.mail_match.UNMATCHED: 1}


@pytest.mark.asyncio
async def test_an_unchanged_user_is_skipped_by_the_handler(f):
    uid = f.make_user()
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC))
    _event(mid, "rejection", company="Nowhere", title="Role")
    _application(uid, company="Elsewhere", title="Other")
    first = f.make_task("match_mail", {"user_id": uid})
    await task.handle_match_mail(first, {"user_id": uid})
    db.execute("UPDATE tasks SET status = 'done', started_at = now() WHERE id = %s", (first,))
    second = f.make_task("match_mail", {"user_id": uid})
    await task.handle_match_mail(second, {"user_id": uid})
    label = db.query_one("SELECT progress->>'label' AS l FROM tasks WHERE id = %s", (second,))
    assert "0 messages swept" in label["l"]
    assert "1 unchanged since the last sweep" in label["l"]


def test_a_partial_sweep_is_not_a_cutoff(f):
    uid = f.make_user()
    for payload in ({"limit": 5}, {"user_id": uid + 1}):
        other = f.make_task("match_mail", payload, status="done")
        db.execute("UPDATE tasks SET started_at = now() WHERE id = %s", (other,))
    assert task.mail_match.last_sweep_start(uid) is None
    assert task.mail_match.last_sweep_start() is None
