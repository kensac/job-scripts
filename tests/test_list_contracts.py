from api import db


def test_task_summary_uses_filters_but_not_page_cursor(client, admin_headers, f):
    older = f.make_task("ingest_source", {"source": "selected"})
    newer = f.make_task("ingest_source", {"source": "selected"})
    f.make_task("ingest_source", {"source": "other"})
    f.make_task("ingest_source", {"source": "selected"}, status="done")
    body = client.get(
        "/v1/admin/tasks",
        params={"source": "selected", "status": "pending", "before_id": newer},
        headers=admin_headers,
    ).json()
    assert [row["id"] for row in body["rows"]] == [older]
    assert body["summary"] == [{"kind": "ingest_source", "status": "pending", "count": 2}]


def test_source_request_rows_and_count_use_user_and_status_sets(client, admin_headers, f):
    mine, other = f.make_user(), f.make_user()
    for uid, status in [(mine, "open"), (mine, "resolved"), (other, "open")]:
        db.execute(
            "INSERT INTO source_requests (user_id, url, status) VALUES (%s, %s, %s)",
            (uid, f"https://requests.test/{uid}/{status}", status),
        )
    body = client.get(
        "/v1/admin/source-requests",
        params={"user": mine, "status": "open,resolved", "limit": 1},
        headers=admin_headers,
    ).json()
    assert body["total"] == 2
    assert len(body["rows"]) == 1 and body["has_more"]
    assert body["rows"][0]["user_id"] == mine
    assert body["filters"] == {"status": ["open", "resolved"], "user": [str(mine)]}


def test_report_status_set_scopes_rows_and_total(client, admin_headers, f):
    uid = f.make_user()
    job = f.make_job()
    for status in ["open", "resolved", "dismissed"]:
        db.execute(
            "INSERT INTO reports (user_id, job_id, kind, status) VALUES (%s, %s, 'other', %s)",
            (uid, job, status),
        )
    body = client.get(
        "/v1/admin/reports", params={"status": "open,resolved"}, headers=admin_headers
    ).json()
    assert body["total"] == 2
    assert {row["status"] for row in body["rows"]} == {"open", "resolved"}
    assert body["filters"]["status"] == ["open", "resolved"]


def test_admin_user_sort_applies_multiple_keys_and_echoes_them(client, admin_headers, f):
    a = f.make_user(email="z@test.invalid")
    b = f.make_user(email="a@test.invalid")
    body = client.get(
        "/v1/admin/users",
        params={"ids": f"{a},{b}", "sort": "name,email", "dir": "asc,asc"},
        headers=admin_headers,
    ).json()
    assert [row["id"] for row in body["users"]] == [b, a]
    assert body["sorts"] == [{"key": "name", "dir": "asc"}, {"key": "email", "dir": "asc"}]


def test_cursor_board_total_does_not_change_when_page_becomes_empty(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    ids = [f.make_job(uploaded_by=uid) for _ in range(3)]
    for cursor in [ids[-1], ids[0]]:
        response = client.get(
            "/v1/user/jobs",
            params={"cursor": cursor, "with_total": True, "sort": "company"},
            headers=user_headers,
        )
        assert response.status_code == 200
        assert response.json()["total"] == 3
        assert response.json()["sorts"] == [{"key": "id", "dir": "desc"}]
