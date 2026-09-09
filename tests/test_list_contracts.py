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


def test_mail_sort_uses_secondary_key(client, admin_headers, f):
    uid = f.make_user()
    ids = []
    for key in ["a", "b"]:
        ids.append(
            db.query_one(
                "INSERT INTO email_messages (user_id, provider_message_id, source, sent_at) "
                "VALUES (%s, %s, 'gmail', '2025-01-01') RETURNING id",
                (uid, key),
            )["id"]
        )
    body = client.get(
        "/v1/admin/mail",
        params={"user": uid, "sort": "sent_at,id", "dir": "asc,asc"},
        headers=admin_headers,
    ).json()
    assert [row["id"] for row in body["rows"]] == ids
    assert body["sorts"] == [{"key": "sent_at", "dir": "asc"}, {"key": "id", "dir": "asc"}]


def test_query_sort_uses_secondary_key(client, admin_headers):
    ids = []
    for status in ["passed", "rejected"]:
        ids.append(
            db.query_one(
                "INSERT INTO ai_queries (url, check_type, status) "
                "VALUES (%s, 'closed', %s) RETURNING id",
                (f"https://sort.test/{status}", status),
            )["id"]
        )
    body = client.get(
        "/v1/admin/queries",
        params={"sort": "check_type,id", "dir": "asc,asc"},
        headers=admin_headers,
    ).json()
    assert [row["id"] for row in body["rows"]] == ids
    assert body["sorts"] == [{"key": "check_type", "dir": "asc"}, {"key": "id", "dir": "asc"}]


def test_board_set_filters_scope_rows_total_and_ats_facets(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    examples = [
        ("a", "greenhouse", "applied"),
        ("b", "lever", "interview"),
        ("a", "ashby", "applied"),
        ("excluded", "lever", "applied"),
        ("a", "lever", "rejected"),
    ]
    ids = []
    for index, (source, ats, status) in enumerate(examples):
        hosts = {
            "greenhouse": "boards.greenhouse.io",
            "lever": "jobs.lever.co",
            "ashby": "jobs.ashbyhq.com",
        }
        job_id = f.make_job(
            url=f"https://{hosts[ats]}/set-contract/{index}", source=source, uploaded_by=uid
        )
        db.execute(
            "INSERT INTO user_jobs(user_id,job_id,status) VALUES (%s,%s,%s)", (uid, job_id, status)
        )
        ids.append(job_id)
    response = client.get(
        "/v1/user/jobs",
        params={
            "statuses": " applied,interview ",
            "sources": "a,b",
            "ats": " GREENHOUSE,lever ",
            "with_total": True,
            "with_facets": True,
            "limit": 1,
        },
        headers=user_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2 and body["has_more"]
    assert body["rows"][0]["job_id"] in ids[:2]
    assert body["filters"] == {
        "status": ["applied", "interview"],
        "source": ["a", "b"],
        "ats": ["greenhouse", "lever"],
    }
    assert {row["ats"]: row["count"] for row in body["facets"]["ats"]} == {
        "greenhouse": 1,
        "lever": 1,
        "ashby": 1,
    }


def test_board_legacy_statuses_alias_merges_into_canonical_echo(client, user_headers, f):
    from api.routers.jobs import NOT_APPLIED

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    jobs = [f.make_job(uploaded_by=uid) for _ in range(3)]
    for job_id, status in zip(jobs, ["applied", None, "rejected"], strict=True):
        db.execute(
            "INSERT INTO user_jobs(user_id,job_id,status) VALUES (%s,%s,%s)", (uid, job_id, status)
        )
    body = client.get(
        "/v1/user/jobs",
        params={"status": "applied", "statuses": f"applied,{NOT_APPLIED}", "with_total": True},
        headers=user_headers,
    ).json()
    assert {row["job_id"] for row in body["rows"]} == set(jobs[:2])
    assert body["total"] == 2
    assert body["filters"] == {"status": ["applied", NOT_APPLIED]}


def test_board_exact_scalar_filters_preserve_commas_alongside_sets(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    ids = []
    for source, status in [
        ("Team, Inc", "Review, later"),
        ("other", "applied"),
        ("Team", "Review"),
    ]:
        job = f.make_job(uploaded_by=uid, source=source)
        db.execute(
            "INSERT INTO user_jobs(user_id,job_id,status) VALUES (%s,%s,%s)", (uid, job, status)
        )
        ids.append(job)
    body = client.get(
        "/v1/user/jobs",
        params={
            "source": "Team, Inc",
            "sources": "other",
            "status": "Review, later",
            "statuses": "applied",
        },
        headers=user_headers,
    ).json()
    assert {row["job_id"] for row in body["rows"]} == set(ids[:2])
    assert body["filters"] == {
        "source": ["other", "Team, Inc"],
        "status": ["applied", "Review, later"],
    }
