from __future__ import annotations

import os

from api import db
from api.worker import enqueue
from core.store import add_ai_result

SERVICE_TOKEN = os.environ["JOBTRACKER_SERVICE_TOKEN"]


def _headers(sub: str, email: str, groups: list) -> dict:
    return {
        "X-Service-Token": SERVICE_TOKEN,
        "X-User-Sub": sub,
        "X-User-Email": email,
        "X-User-Name": sub,
        "X-User-Groups": ",".join(groups),
    }


def _bootstrap(client, sub: str, groups=("jobtracker-users-internal",)) -> dict:
    headers = _headers(sub, f"{sub}@example.com", list(groups))
    resp = client.post("/v1/users/bootstrap", headers=headers)
    assert resp.status_code == 200, resp.text
    return headers


def test_admin_route_forbidden_for_regular_user(client, user_headers):
    resp = client.get("/v1/admin/users", headers=user_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "FORBIDDEN"


def test_admin_users_pagination(client, admin_headers):
    _bootstrap(client, "user-a")
    _bootstrap(client, "user-b")

    page1 = client.get("/v1/admin/users", params={"limit": 2}, headers=admin_headers)
    data1 = page1.json()
    assert len(data1["users"]) == 2
    assert data1["has_more"] is True

    page2 = client.get("/v1/admin/users", params={"limit": 2, "offset": 2}, headers=admin_headers)
    data2 = page2.json()
    assert len(data2["users"]) == 1
    assert data2["has_more"] is False


def test_admin_tasks_before_id_cursor(client, admin_headers):
    ids = [enqueue("run_filter", {"i": i}) for i in range(5)]
    assert all(ids)

    page1 = client.get("/v1/admin/tasks", params={"limit": 2}, headers=admin_headers)
    data1 = page1.json()
    got1 = [r["id"] for r in data1["rows"]]
    assert got1 == sorted(got1, reverse=True)
    assert data1["has_more"] is True

    lowest = got1[-1]
    page2 = client.get(
        "/v1/admin/tasks", params={"limit": 2, "before_id": lowest}, headers=admin_headers
    )
    data2 = page2.json()
    got2 = [r["id"] for r in data2["rows"]]
    assert all(i < lowest for i in got2)
    assert not (set(got1) & set(got2))


def test_admin_source_requests_pagination(client, admin_headers, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", (user_headers["X-User-Sub"],))["id"]
    for i in range(3):
        db.execute(
            "INSERT INTO source_requests (user_id, url) VALUES (%s, %s)",
            (uid, f"https://board{i}.example.com"),
        )

    page1 = client.get(
        "/v1/admin/source-requests", params={"status": "open", "limit": 2}, headers=admin_headers
    )
    data1 = page1.json()
    assert len(data1["rows"]) == 2
    assert data1["has_more"] is True

    page2 = client.get(
        "/v1/admin/source-requests",
        params={"status": "open", "limit": 2, "offset": 2},
        headers=admin_headers,
    )
    data2 = page2.json()
    assert len(data2["rows"]) == 1
    assert data2["has_more"] is False


def test_admin_failures_breakdown_grouped_by_worker_and_host(client, admin_headers):
    add_ai_result("https://hosta.example.com/1", "failed", check_type="closed", error="boom")
    add_ai_result("https://hosta.example.com/2", "failed", check_type="closed", error="boom")
    add_ai_result("https://hostb.example.com/1", "failed", check_type="clearance", error="boom2")

    resp = client.get("/v1/admin/failures", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    by_host = {(r["host"], r["check_type"]): r["failures"] for r in rows}
    assert by_host[("hosta.example.com", "closed")] == 2
    assert by_host[("hostb.example.com", "clearance")] == 1
    assert all(r["worker"] for r in rows)


def test_admin_signups_toggle_blocks_new_but_not_existing(client, admin_headers, user_headers):
    toggled = client.put(
        "/v1/admin/config/signups_enabled", json={"value": False}, headers=admin_headers
    )
    assert toggled.status_code == 200

    new_headers = _headers("brand-new-user", "new@example.com", ["jobtracker-users-public"])
    blocked = client.post("/v1/users/bootstrap", headers=new_headers)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "SIGNUPS_DISABLED"

    # an already-registered user (created by the user_headers fixture before
    # the toggle) is unaffected.
    still_ok = client.post("/v1/users/bootstrap", headers=user_headers)
    assert still_ok.status_code == 200


def test_admin_group_budgets_roundtrip(client, admin_headers):
    put = client.put(
        "/v1/admin/group-budgets/test-group",
        json={"weekly_token_budget": 12345, "allowed_models": ["gpt-5-nano"]},
        headers=admin_headers,
    )
    assert put.status_code == 200

    listed = client.get("/v1/admin/group-budgets", headers=admin_headers)
    assert listed.status_code == 200
    data = listed.json()
    row = next(g for g in data["groups"] if g["group_name"] == "test-group")
    assert row["weekly_token_budget"] == 12345
    assert row["allowed_models"] == ["gpt-5-nano"]
    assert "gpt-5-nano" in data["catalog_models"]


def test_cancel_tasks_cancels_only_live_states(client, admin_headers):
    from api import worker
    t1 = worker.enqueue("run_filter", {"user_id": 1})
    t2 = worker.enqueue("run_filter", {"user_id": 2})
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (t2,))
    resp = client.post(
        "/v1/admin/tasks/cancel", json={"ids": [t1, t2, 999999]}, headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] == [t1]
    assert set(body["skipped"]) == {t2, 999999}
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (t1,))
    assert row["status"] == "cancelled" and "admin" in row["error"]
    assert db.query_one("SELECT status FROM tasks WHERE id = %s", (t2,))["status"] == "done"


def test_invites_unconfigured_returns_503_and_empty_list(client, admin_headers):
    resp = client.post("/v1/admin/invites", json={"email": "new@user.com"}, headers=admin_headers)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "INVITES_NOT_CONFIGURED"
    resp = client.get("/v1/admin/invites", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == {"rows": [], "configured": False}


def test_invite_rejects_bad_email(client, admin_headers):
    resp = client.post("/v1/admin/invites", json={"email": "not-an-email"}, headers=admin_headers)
    assert resp.status_code == 422
