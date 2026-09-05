"""Every admin list with a user dimension takes user=<id>[,<id>] and narrows
rows, summaries and totals together; the envelope names its filters."""

from __future__ import annotations

import datetime

from api import db
from api.tasks import runtime as tasks_runtime
from tests.test_api_jobs import _insert_job


def _uid(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


def test_reports_tasks_batches_scope_by_user(
    client, admin_headers, user_headers, other_user_headers
):
    me, other = _uid(user_headers), _uid(other_user_headers)
    j1 = _insert_job("src-sc", "https://x.test/sc1")
    j2 = _insert_job("src-sc", "https://x.test/sc2")
    assert (
        client.post(
            f"/v1/user/jobs/{j1}/report", json={"kind": "other"}, headers=user_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/user/jobs/{j2}/report", json={"kind": "other"}, headers=other_user_headers
        ).status_code
        == 200
    )
    body = client.get("/v1/admin/reports", params={"user": me}, headers=admin_headers).json()
    assert {r["job_id"] for r in body["rows"]} == {j1} and body["total"] == 1
    assert body["filters"] == {"status": ["open"], "user": [str(me)]}
    assert "user" in body["filterable"]
    both = client.get(
        "/v1/admin/reports", params={"user": f"{me},{other}"}, headers=admin_headers
    ).json()
    assert {r["job_id"] for r in both["rows"]} == {j1, j2}

    mine = tasks_runtime.enqueue("run_filter", {"user_id": me, "filter_id": 1})
    theirs = tasks_runtime.enqueue("run_filter", {"user_id": other, "filter_id": 2})
    fleet = tasks_runtime.enqueue("ingest_source", {"source": "acme"})
    body = client.get("/v1/admin/tasks", params={"user": me}, headers=admin_headers).json()
    assert [r["id"] for r in body["rows"]] == [mine]
    assert sum(s["count"] for s in body["summary"]) == 1
    assert body["filters"] == {"user": [str(me)]} and "user" in body["filterable"]
    everything = {
        r["id"] for r in client.get("/v1/admin/tasks", headers=admin_headers).json()["rows"]
    }
    assert {mine, theirs, fleet} <= everything

    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, task_id, purpose, model, requests, status, submitted_at) "
        "VALUES ('b-mine', %s, 'filters', 'm', 1, 'completed', now()), "
        "('b-fleet', %s, 'requirements', 'm', 1, 'completed', now())",
        (mine, fleet),
    )
    body = client.get("/v1/admin/batches", params={"user": me}, headers=admin_headers).json()
    assert [r["provider_batch_id"] for r in body["rows"]] == ["b-mine"]
    assert body["filterable"] == ["user"]


def test_mail_spend_and_queries_scope_by_user(
    client, admin_headers, user_headers, other_user_headers
):
    me, other = _uid(user_headers), _uid(other_user_headers)
    sent = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
    for uid, key in ((me, "sc-a"), (other, "sc-b")):
        db.execute(
            "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
            "VALUES (%s, %s, 'gmail', 's', %s)",
            (uid, key, sent),
        )
    body = client.get("/v1/admin/mail", params={"user": me}, headers=admin_headers).json()
    assert [r["provider_message_id"] for r in body["rows"]] == ["sc-a"] and body["total"] == 1
    assert body["filters"] == {"user": [str(me)]} and "user" in body["filterable"]

    for uid, cost in ((me, 1.5), (other, 2.5)):
        db.execute(
            "INSERT INTO api_usage (user_id, key_source, purpose, model, prompt_tokens, "
            "completion_tokens, total_tokens, cost_usd) VALUES (%s, 'server', 'filters', 'm', 1, 1, 2, %s)",
            (uid, cost),
        )
    body = client.get("/v1/admin/spend/calls", params={"user": me}, headers=admin_headers).json()
    assert body["totals"]["calls"] == 1 and float(body["totals"]["cost_usd"]) == 1.5
    assert body["filters"] == {"user": [str(me)]} and "user" in body["filterable"]

    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash, enabled) "
        "VALUES (%s, 'f', 'p', 'hash-mine', true), (%s, 'g', 'q', 'hash-theirs', true)",
        (me, other),
    )
    db.execute(
        "INSERT INTO ai_queries (url, check_type, status, prompt_hash, reason) VALUES "
        "('https://x.test/q1', 'custom', 'rejected', 'hash-mine', 'no'), "
        "('https://x.test/q2', 'custom', 'rejected', 'hash-theirs', 'no'), "
        "('https://x.test/q3', 'closed', 'passed', NULL, NULL)"
    )
    body = client.get("/v1/admin/queries", params={"user": me}, headers=admin_headers).json()
    assert [r["url"] for r in body["rows"]] == ["https://x.test/q1"] and body["total"] == 1
    assert body["filters"] == {"user": [str(me)]} and "user" in body["filterable"]
    unscoped = client.get("/v1/admin/queries", headers=admin_headers).json()
    assert unscoped["filters"] == {} and unscoped["total"] >= 3

    body = client.get(
        "/v1/admin/filter-insights/rejection-reasons",
        params={"user": me, "min_decisions": 1},
        headers=admin_headers,
    ).json()
    assert [v["prompt_hash"] for v in body["prompt_versions"]] == ["hash-mine"]
    assert body["filters"] == {"user": [str(me)]} and body["filterable"] == ["prompt_hash", "user"]
    body = client.get(
        "/v1/admin/filter-insights/phrasings",
        params={"prompt_hash": "hash-theirs", "user": me},
        headers=admin_headers,
    ).json()
    assert body["total_decisions"] == 0 and "user" in body["filterable"]


def test_a_non_numeric_user_is_ignored_not_refused(client, admin_headers):
    body = client.get("/v1/admin/tasks", params={"user": "nope"}, headers=admin_headers).json()
    assert body["filters"] == {}
