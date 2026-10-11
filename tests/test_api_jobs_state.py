from __future__ import annotations

import datetime

from api import db
from api.mail import applications
from tests.job_board_support import _insert_job, _pass_closed, _subscribe, _uid


def test_delete_user_job_removes_only_user_jobs_row(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-i", "https://x.test/i1")
    client.patch(f"/v1/user/jobs/{jid}", json={"notes": "x"}, headers=user_headers)
    assert db.query_one("SELECT 1 FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))

    resp = client.delete(f"/v1/user/jobs/{jid}", headers=user_headers)
    assert resp.status_code == 200
    assert (
        db.query_one("SELECT 1 FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))
        is None
    )
    assert db.query_one("SELECT 1 FROM jobs WHERE id = %s", (jid,)) is not None


def _applied_day(uid: int, jid: int) -> datetime.date | None:
    """The board's applied day, which is the application's."""
    return applications.board_day(uid, jid)


def _has_application(uid: int, jid: int) -> bool:
    return (
        db.query_one("SELECT 1 FROM applications WHERE user_id = %s AND job_id = %s", (uid, jid))
        is not None
    )


def test_patch_status_autofills_date_applied(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af1")
    resp = client.patch(
        f"/v1/user/jobs/{jid}", json={"status": "Application Submitted"}, headers=user_headers
    )
    assert resp.status_code == 200
    # UTC, matching the server: the container's local date is an arbitrary
    # timezone to decide a user's "today" in.
    today = datetime.datetime.now(datetime.UTC).date()
    assert resp.json()["autofilled"] == {"date_applied": today.isoformat()}
    assert _applied_day(uid, jid) == today


def test_patch_withdrawn_status_gets_no_applied_day(client, user_headers):
    """A posting the person decided against was never applied to."""
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af5")
    resp = client.patch(
        f"/v1/user/jobs/{jid}", json={"status": "No Longer Interested"}, headers=user_headers
    )
    assert resp.json()["autofilled"] == {}
    assert _applied_day(uid, jid) is None
    assert not _has_application(uid, jid)


def test_patch_explicit_date_applied_wins_over_autofill(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af2")
    resp = client.patch(
        f"/v1/user/jobs/{jid}",
        json={"status": "Application Submitted", "date_applied": "2026-08-01"},
        headers=user_headers,
    )
    assert resp.json()["autofilled"] == {}
    assert _applied_day(uid, jid) == datetime.date(2026, 8, 1)
    rows = client.get("/v1/user/jobs", headers=user_headers).json()["rows"]
    assert [r["date_applied"] for r in rows if r["job_id"] == jid] == ["2026-08-01"]


def test_patch_status_change_does_not_overwrite_existing_date_applied(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af3")
    client.patch(
        f"/v1/user/jobs/{jid}",
        json={"status": "Application Submitted", "date_applied": "2026-08-01"},
        headers=user_headers,
    )
    resp = client.patch(f"/v1/user/jobs/{jid}", json={"status": "Follow-up"}, headers=user_headers)
    assert resp.json()["autofilled"] == {}
    assert _applied_day(uid, jid) == datetime.date(2026, 8, 1)


def test_patch_date_edits_and_clears_the_application_day(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af6")
    client.patch(
        f"/v1/user/jobs/{jid}", json={"status": "Application Submitted"}, headers=user_headers
    )
    client.patch(f"/v1/user/jobs/{jid}", json={"date_applied": "2026-07-04"}, headers=user_headers)
    assert _applied_day(uid, jid) == datetime.date(2026, 7, 4)
    client.patch(f"/v1/user/jobs/{jid}", json={"date_applied": None}, headers=user_headers)
    assert _applied_day(uid, jid) is None
    # Clearing the day keeps the application.
    assert _has_application(uid, jid)


def test_patch_notes_only_does_not_autofill_date_applied(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af4")
    resp = client.patch(f"/v1/user/jobs/{jid}", json={"notes": "check later"}, headers=user_headers)
    assert resp.json()["autofilled"] == {}
    assert _applied_day(uid, jid) is None


def test_status_changes_append_history(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-h", "https://x.test/h1")
    client.patch(f"/v1/user/jobs/{jid}", json={"status": "Applied"}, headers=user_headers)
    client.patch(f"/v1/user/jobs/{jid}", json={"status": "Interview"}, headers=user_headers)
    client.patch(f"/v1/user/jobs/{jid}", json={"notes": "n"}, headers=user_headers)
    rows = db.query(
        "SELECT old_status, new_status FROM user_job_history WHERE user_id = %s AND job_id = %s ORDER BY id",
        (uid, jid),
    )
    assert [(r["old_status"], r["new_status"]) for r in rows] == [
        (None, "Applied"),
        ("Applied", "Interview"),
    ]


def test_a_bulk_patch_publishes_one_event_not_one_per_row(client, user_headers, monkeypatch):
    """The bulk endpoint exists so a large selection is one request; the
    per-row publish is a synchronous post on the request path, so it must
    not become one post per row either. One event carries the ids."""
    from api import events

    published: list[dict] = []
    monkeypatch.setattr(events, "_publish", lambda channel, data: published.append(data))
    uid = _uid(user_headers)
    ids = [_insert_job("src-bulk", f"https://x.test/bulk{i}") for i in range(3)]
    _subscribe(uid, "src-bulk")
    for i in range(3):
        _pass_closed(f"https://x.test/bulk{i}")
    res = client.patch(
        "/v1/user/jobs",
        json={"job_ids": ids, "patch": {"status": "No Longer Interested"}},
        headers=user_headers,
    )
    assert res.status_code == 200 and res.json()["updated"] == 3
    kinds = [d["type"] for d in published]
    assert kinds.count("board_rows") == 1 and kinds.count("board_row") == 0
    event = next(d for d in published if d["type"] == "board_rows")
    assert sorted(event["job_ids"]) == sorted(ids) and event["status"] == "No Longer Interested"
