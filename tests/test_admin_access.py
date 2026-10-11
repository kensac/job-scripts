from __future__ import annotations

import os

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


def test_closed_signups_admit_the_configured_admin_groups(client, admin_headers, monkeypatch):
    from api import auth

    client.put("/v1/admin/config/signups_enabled", json={"value": False}, headers=admin_headers)
    monkeypatch.setattr(auth, "ADMIN_GROUPS", frozenset({"ops"}))
    renamed = client.post(
        "/v1/users/bootstrap", headers=_headers("ops-admin", "ops@example.com", ["ops"])
    )
    assert renamed.status_code == 200
    stale = client.post(
        "/v1/users/bootstrap",
        headers=_headers("old-admin", "old@example.com", ["infra-admins"]),
    )
    assert stale.status_code == 403


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
