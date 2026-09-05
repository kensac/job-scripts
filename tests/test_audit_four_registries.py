"""Vocabularies served with their meaning, and set-shaped filters as lists
with a `filters` echo, so the frontend keeps no parallel copy of either."""

from __future__ import annotations

import datetime

from api import db
from api.tasks import runtime as tasks_runtime
from tests.test_api_pipeline import _app


def _uid(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


def test_board_options_carry_status_meaning_and_report_kinds(client, user_headers):
    body = client.get("/v1/user/jobs/options", headers=user_headers).json()
    meta = {m["name"]: m for m in body["status_meta"]}
    assert set(meta) == set(body["statuses"])
    assert meta["Accepted"] == {"name": "Accepted", "terminal": True, "outcome": "won"}
    assert meta["Rejected"]["outcome"] == "lost"
    assert meta["No Longer Interested"]["outcome"] == "withdrawn"
    assert meta["Interview"] == {"name": "Interview", "terminal": False, "outcome": None}
    assert [k["kind"] for k in body["report_kinds"]] == ["stale", "wrong_data", "closed", "other"]
    assert all(k["label"] for k in body["report_kinds"])


def test_admin_reports_carry_the_same_report_kinds(client, admin_headers):
    body = client.get("/v1/admin/reports", headers=admin_headers).json()
    assert [k["kind"] for k in body["report_kinds"]] == ["stale", "wrong_data", "closed", "other"]


def test_queue_takes_lists_echoes_filters_and_flags_cancellable(client, admin_headers):
    pending = tasks_runtime.enqueue("run_filter", {"a": 1})
    done = tasks_runtime.enqueue("run_filter", {"a": 2})
    parked = tasks_runtime.enqueue("ingest_source", {"source": "acme"})
    db.execute("UPDATE tasks SET status = 'done', worker = 'w-a' WHERE id = %s", (done,))
    db.execute(
        "UPDATE tasks SET status = 'awaiting_batch', worker = 'w-b' WHERE id = %s", (parked,)
    )

    body = client.get(
        "/v1/admin/tasks", params={"status": "pending,done"}, headers=admin_headers
    ).json()
    assert {r["id"] for r in body["rows"]} == {pending, done}
    assert body["filters"] == {"status": ["pending", "done"]}
    by_id = {r["id"]: r for r in body["rows"]}
    assert by_id[pending]["cancellable"] is True and by_id[done]["cancellable"] is False
    assert body["statuses"] == [
        "pending",
        "waiting",
        "running",
        "awaiting_batch",
        "done",
        "failed",
        "cancelled",
    ]

    body = client.get("/v1/admin/tasks", params={"worker": "w-b"}, headers=admin_headers).json()
    assert [r["id"] for r in body["rows"]] == [parked]
    assert body["rows"][0]["cancellable"] is True
    assert body["filters"] == {"worker": ["w-b"]}

    body = client.get("/v1/admin/tasks", headers=admin_headers).json()
    assert body["filters"] == {}


def test_admin_mail_takes_lists_on_kind_method_and_source(client, admin_headers, user_headers):
    uid = _uid(user_headers)
    sent = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
    ids = {}
    for key, source, kind in (
        ("a", "gmail", "rejection"),
        ("b", "takeout", "interview_invite"),
        ("c", "gmail", "acknowledgement"),
    ):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
            "VALUES (%s, %s, %s, 's', %s) RETURNING id",
            (uid, f"reg-{key}", source, sent),
        )["id"]
        db.execute("INSERT INTO email_events (message_id, kind) VALUES (%s, %s)", (mid, kind))
        ids[key] = mid
    body = client.get(
        "/v1/admin/mail",
        params={"kind": "rejection,interview_invite", "source": "gmail,takeout"},
        headers=admin_headers,
    ).json()
    assert {r["id"] for r in body["rows"]} == {ids["a"], ids["b"]}
    assert body["filters"] == {
        "kind": ["rejection", "interview_invite"],
        "source": ["gmail", "takeout"],
    }
    body = client.get(
        "/v1/admin/mail", params={"kind": "acknowledgement"}, headers=admin_headers
    ).json()
    assert [r["id"] for r in body["rows"]] == [ids["c"]]
    body = client.get(
        "/v1/admin/mail", params={"method": "never_attempted,ats_company"}, headers=admin_headers
    ).json()
    assert {r["id"] for r in body["rows"]} >= set(ids.values())
    assert body["filters"]["method"] == ["never_attempted", "ats_company"]


def test_pipeline_takes_provenance_lists_and_serves_stages(client, user_headers):
    uid = _uid(user_headers)
    tracker = _app(uid, company="A", prov="tracker")
    email = _app(uid, company="B", prov="email")
    body = client.get(
        "/v1/user/pipeline", params={"provenance": "tracker,email"}, headers=user_headers
    ).json()
    assert {a["id"] for a in body["applications"]} >= {tracker, email}
    assert body["filters"] == {"provenance": ["tracker", "email"]}
    body = client.get(
        "/v1/user/pipeline", params={"provenance": "email"}, headers=user_headers
    ).json()
    got = {a["id"] for a in body["applications"]}
    assert email in got and tracker not in got

    summary = client.get("/v1/user/pipeline/summary", headers=user_headers).json()
    stages = {s["key"]: s for s in summary["stages"]}
    assert [s["key"] for s in summary["stages"]][:2] == ["applied", "acknowledged"]
    assert stages["rejected"]["terminal"] and stages["withdrawn"]["terminal"]
    assert not stages["interviewing"]["terminal"] and stages["offer"]["label"] == "Offer"
