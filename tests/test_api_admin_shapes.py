"""List shapes the admin pages asked for after the third UI sweep: a stated
has_more on mail, a bulk user lookup, and cheap sources shapes for pickers
and dashboard tiles."""

from __future__ import annotations

from api import db


def test_mail_list_states_has_more(client, admin_headers):
    body = client.get("/v1/admin/mail", params={"page_size": 1}, headers=admin_headers).json()
    assert body["has_more"] is (body["total"] > 1)


def test_users_bulk_lookup_by_ids(client, admin_headers, user_headers):
    ids = [r["id"] for r in db.query("SELECT id FROM users ORDER BY id")]
    assert len(ids) >= 2
    body = client.get(
        "/v1/admin/users", params={"ids": f"{ids[0]},junk,{ids[-1]}"}, headers=admin_headers
    ).json()
    assert {u["id"] for u in body["users"]} == {ids[0], ids[-1]}
    # An empty selection is an empty answer, not the whole ledger.
    assert (
        client.get("/v1/admin/users", params={"ids": ""}, headers=admin_headers).json()["users"]
        == []
    )


def test_sources_shapes(client, admin_headers, f):
    f.make_source("acme")
    db.execute(
        "UPDATE sources SET listings_url = 'https://boards-api.greenhouse.io/v1/boards/acme/jobs' "
        "WHERE name = 'acme'"
    )
    f.make_source("dormant", active=False)
    db.execute(
        "INSERT INTO source_groups (name, members) VALUES ('boards', ARRAY['acme']) "
        "ON CONFLICT (name) DO UPDATE SET members = EXCLUDED.members"
    )

    names = client.get("/v1/admin/sources", params={"shape": "names"}, headers=admin_headers).json()
    by_name = {s["name"]: s for s in names["sources"]}
    assert set(by_name["acme"]) == {"name", "active", "kind", "groups"}
    assert by_name["acme"]["kind"] == "greenhouse" and by_name["acme"]["groups"] == ["boards"]
    assert by_name["dormant"]["active"] is False and by_name["dormant"]["groups"] == []

    counts = client.get(
        "/v1/admin/sources", params={"shape": "counts"}, headers=admin_headers
    ).json()
    assert counts["sources"] >= 2 and counts["active"] == counts["sources"] - 1
    assert counts["by_kind"]["greenhouse"] == 1
    assert counts["bundles"] >= 1 and isinstance(counts["last_ingest"], dict)

    full = client.get("/v1/admin/sources", headers=admin_headers).json()
    assert "jobs" in full["sources"][0]


def test_digest_unsubscribe_accepts_post(client, user_headers):
    client.put("/v1/user/settings", json={"email_digest": True}, headers=user_headers)
    row = db.query_one(
        "SELECT digest_token FROM user_settings us JOIN users u ON u.id = us.user_id "
        "WHERE u.sub = %s",
        (user_headers["X-User-Sub"],),
    )
    assert row is not None and row["digest_token"]
    service_only = {"X-Service-Token": user_headers["X-Service-Token"]}
    r = client.post(f"/v1/digest/unsubscribe?token={row['digest_token']}", headers=service_only)
    assert r.status_code == 200
    off = db.query_one(
        "SELECT email_digest FROM user_settings WHERE digest_token = %s", (row["digest_token"],)
    )
    assert off is not None and off["email_digest"] is False
