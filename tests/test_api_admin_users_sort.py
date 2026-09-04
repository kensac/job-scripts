"""GET /admin/users sorts server-side on whitelisted keys and says which."""

from __future__ import annotations

from api import db


def test_users_sort_by_a_computed_column_and_echo_the_sort(client, admin_headers, user_headers):
    admin = db.query_one("SELECT id FROM users WHERE sub = 'test-admin'")
    other = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert admin is not None and other is not None
    # Give the plain user the bigger board so the sort is visibly not by id.
    for i in range(3):
        job = db.query_one(
            "INSERT INTO jobs (url, source) VALUES (%s, 'src-test') RETURNING id",
            (f"https://sort.test/{i}",),
        )
        assert job is not None
        db.execute(
            "INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s)", (other["id"], job["id"])
        )
    body = client.get(
        "/v1/admin/users", params={"sort": "board_rows", "dir": "desc"}, headers=admin_headers
    ).json()
    assert body["users"][0]["id"] == other["id"]
    assert (body["sort"], body["dir"]) == ("board_rows", "desc")
    assert "board_rows" in body["sortable"] and "email" in body["sortable"]

    body = client.get(
        "/v1/admin/users", params={"sort": "board_rows", "dir": "asc"}, headers=admin_headers
    ).json()
    assert body["users"][-1]["id"] == other["id"]

    # An unknown key falls back to the default and says so.
    body = client.get("/v1/admin/users", params={"sort": "nope"}, headers=admin_headers).json()
    assert body["sort"] == "last_seen_at"
