from __future__ import annotations

import pytest

from api import db


def _board(slug: str = "engineering", *, published: bool = True) -> int:
    owner = db.query_one(
        "INSERT INTO users (sub, email) VALUES (%s, 'owner@example.com') RETURNING id",
        (f"owner-{slug}",),
    )
    assert owner is not None
    board = db.query_one(
        "INSERT INTO managed_boards "
        "(slug, name, description, sponsor_user_id, prompt, prompt_hash, requested_model, "
        "published, public_revision, published_at, projection_updated_at) "
        "VALUES (%s, %s, 'Selected roles', %s, 'prompt', 'hash', 'gpt-5.6-luna', %s, "
        "1, now(), now()) RETURNING id",
        (slug, slug.title(), owner["id"], published),
    )
    assert board is not None
    return board["id"]


def _job(index: int, board_id: int | None) -> int:
    job = db.query_one(
        "INSERT INTO jobs (url, company, title, locations, terms, source, active) "
        "VALUES (%s, 'Example', 'Engineer', ARRAY['New York'], ARRAY['full-time'], "
        "'public-test', true) RETURNING id",
        (f"https://boards.greenhouse.io/example/jobs/{index}",),
    )
    assert job is not None
    if board_id is not None:
        db.execute(
            "INSERT INTO managed_board_jobs (managed_board_id, job_id, sort_at) "
            "VALUES (%s, %s, now())",
            (board_id, job["id"]),
        )
    return job["id"]


def _signed_in_report(user_headers: dict, job_id: int) -> None:
    """A person's own report, written as `POST /user/jobs/{id}/report` writes
    it, so the test does not depend on the posting reaching their board."""
    db.execute(
        "INSERT INTO reports (user_id, job_id, kind) "
        "SELECT id, %s, 'stale' FROM users WHERE sub = %s",
        (job_id, user_headers["X-User-Sub"]),
    )


def _users() -> int:
    row = db.query_one("SELECT count(*) AS n FROM users")
    assert row is not None
    return row["n"]


def test_a_list_note_is_stored_anonymously_and_reaches_the_inbox(client, admin_headers):
    _board()
    users = _users()

    sent = client.post(
        "/v1/public/job-lists/engineering/feedback",
        json={"message": "  More roles in Toronto, please  ", "contact": " me@example.com "},
    )

    assert sent.status_code == 200, sent.text
    assert _users() == users
    inbox = client.get("/v1/admin/job-list-feedback", headers=admin_headers).json()
    [note] = inbox["rows"]
    assert set(sent.json()) == {"created_at"}
    assert note["message"] == "More roles in Toronto, please"
    assert note["contact"] == "me@example.com"
    assert (note["job_list_slug"], note["job_list_name"]) == ("engineering", "Engineering")
    assert note["status"] == "open"

    done = client.post(
        f"/v1/admin/job-list-feedback/{note['id']}/resolve",
        json={"action": "resolved", "note": "added a Toronto source"},
        headers=admin_headers,
    )
    assert done.json() == {"id": note["id"], "status": "resolved"}
    assert client.get("/v1/admin/job-list-feedback", headers=admin_headers).json()["rows"] == []
    every = client.get("/v1/admin/job-list-feedback?status=all", headers=admin_headers).json()
    assert every["rows"][0]["resolution_note"] == "added a Toronto source"

    again = client.post(
        f"/v1/admin/job-list-feedback/{note['id']}/resolve",
        json={"action": "dismissed"},
        headers=admin_headers,
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ALREADY_ANSWERED"
    kept = client.get("/v1/admin/job-list-feedback?status=all", headers=admin_headers).json()
    assert kept["rows"][0]["status"] == "resolved"
    assert kept["rows"][0]["resolution_note"] == "added a Toronto source"


def test_the_inbox_is_admin_only(client, user_headers):
    response = client.get("/v1/admin/job-list-feedback", headers=user_headers)
    assert response.status_code == 403


@pytest.mark.parametrize("published", [False, None])
def test_unknown_and_unpublished_lists_take_nothing(client, published):
    if published is not None:
        _board(published=published)

    response = client.post("/v1/public/job-lists/engineering/feedback", json={"message": "hello"})

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"
    assert db.query("SELECT id FROM job_list_feedback") == []


@pytest.mark.parametrize(
    "body",
    [
        {"message": "   "},
        {"message": "x" * 4001},
        {"message": "hello", "contact": "not an address"},
        {"message": "hello", "contact": "a@b.c" + "x" * 254},
    ],
)
def test_a_note_that_says_nothing_or_names_no_address_is_refused(client, body):
    _board()
    response = client.post("/v1/public/job-lists/engineering/feedback", json=body)
    assert response.status_code == 422
    assert db.query("SELECT id FROM job_list_feedback") == []


def test_a_blank_contact_is_stored_as_none_left(client):
    _board()
    response = client.post(
        "/v1/public/job-lists/engineering/feedback", json={"message": "hi", "contact": "  "}
    )
    assert response.status_code == 200
    assert db.query("SELECT contact FROM job_list_feedback") == [{"contact": None}]


def test_a_posting_report_joins_the_report_queue_and_queues_no_recheck(client, admin_headers):
    board_id = _board()
    job_id = _job(1, board_id)
    users = _users()

    sent = client.post(
        f"/v1/public/job-lists/engineering/jobs/{job_id}/report",
        json={"kind": "closed", "message": "404s now", "contact": "me@example.com"},
    )

    assert sent.status_code == 200, sent.text
    assert _users() == users
    assert db.query("SELECT id FROM tasks WHERE kind = 'reverify_chunk'") == []
    queue = client.get("/v1/admin/reports", headers=admin_headers).json()
    [report] = queue["rows"]
    assert set(sent.json()) == {"created_at"}
    assert report["job_id"] == job_id
    assert report["user_id"] is None
    assert report["reporter_email"] is None
    assert report["job_list_slug"] == "engineering"
    assert report["contact"] == "me@example.com"
    assert report["kind"] == "closed"


def test_a_signed_in_report_still_reads_with_its_person(client, admin_headers, user_headers):
    board_id = _board()
    job_id = _job(1, board_id)
    _signed_in_report(user_headers, job_id)

    [report] = client.get("/v1/admin/reports", headers=admin_headers).json()["rows"]

    assert report["reporter_email"] == "user@example.com"
    assert report["job_list_slug"] is None
    assert report["contact"] is None


def test_a_report_names_a_posting_on_that_published_list(client):
    _board()
    other_board = _board("design")
    off_list = _job(1, None)
    elsewhere = _job(2, other_board)
    hidden_board = _board("hidden", published=False)
    hidden = _job(3, hidden_board)

    for slug, job_id in [
        ("engineering", off_list),
        ("engineering", elsewhere),
        ("hidden", hidden),
        ("engineering", 999_999),
    ]:
        response = client.post(
            f"/v1/public/job-lists/{slug}/jobs/{job_id}/report", json={"kind": "stale"}
        )
        assert response.status_code == 404, (slug, job_id)
    assert db.query("SELECT id FROM reports") == []


@pytest.mark.parametrize(
    "body,code",
    [
        ({"kind": "fake"}, "INVALID_KIND"),
        ({"kind": "other"}, "MESSAGE_REQUIRED"),
        ({"kind": "wrong_data", "message": "  "}, "MESSAGE_REQUIRED"),
    ],
)
def test_a_report_must_name_a_kind_and_say_what_else(client, body, code):
    job_id = _job(1, _board())
    response = client.post(f"/v1/public/job-lists/engineering/jobs/{job_id}/report", json=body)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == code
    assert db.query("SELECT id FROM reports") == []


def test_the_hourly_limit_counts_notes_and_reports_across_lists(client, set_config):
    set_config("public_feedback_hourly_limit", 2)
    job_id = _job(1, _board())
    _board("design")
    db.execute(
        "INSERT INTO job_list_feedback (managed_board_id, message, created_at) "
        "SELECT id, 'last week', now() - interval '2 hours' FROM managed_boards LIMIT 1"
    )

    note = client.post("/v1/public/job-lists/design/feedback", json={"message": "a"})
    report = client.post(
        f"/v1/public/job-lists/engineering/jobs/{job_id}/report", json={"kind": "stale"}
    )
    refused = client.post("/v1/public/job-lists/engineering/feedback", json={"message": "b"})

    assert (note.status_code, report.status_code) == (200, 200)
    assert refused.status_code == 429
    assert refused.json()["detail"]["code"] == "FEEDBACK_BUSY"
    count = db.query_one("SELECT count(*) AS n FROM job_list_feedback")
    assert count is not None and count["n"] == 2


def test_a_zero_limit_closes_intake(client, set_config):
    set_config("public_feedback_hourly_limit", 0)
    job_id = _job(1, _board())

    note = client.post("/v1/public/job-lists/engineering/feedback", json={"message": "a"})
    report = client.post(
        f"/v1/public/job-lists/engineering/jobs/{job_id}/report", json={"kind": "stale"}
    )

    assert note.status_code == report.status_code == 503
    assert note.json()["detail"]["code"] == "FEEDBACK_CLOSED"
    assert db.query("SELECT id FROM job_list_feedback UNION ALL SELECT id FROM reports") == []


def test_a_signed_in_persons_reports_do_not_count_toward_the_limit(
    client, set_config, user_headers
):
    set_config("public_feedback_hourly_limit", 1)
    job_id = _job(1, _board())
    _signed_in_report(user_headers, job_id)

    response = client.post("/v1/public/job-lists/engineering/feedback", json={"message": "a"})

    assert response.status_code == 200


def test_the_public_job_detail_serves_the_report_vocabulary(client):
    job_id = _job(1, _board())
    detail = client.get(f"/v1/public/job-lists/engineering/jobs/{job_id}")
    assert detail.status_code == 200
    kinds = {k["kind"]: k["message_required"] for k in detail.json()["report_kinds"]}
    assert kinds == {"stale": False, "wrong_data": True, "closed": False, "other": True}
