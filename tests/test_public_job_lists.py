from __future__ import annotations

import datetime

from api import db


def _published_board() -> int:
    user = db.query_one(
        "INSERT INTO users (sub, email) VALUES ('public-board-owner', 'owner@example.com') "
        "RETURNING id"
    )
    assert user is not None
    board = db.query_one(
        "INSERT INTO managed_boards "
        "(slug, name, description, sponsor_user_id, prompt, prompt_hash, requested_model, "
        "published, public_revision, published_at, projection_updated_at) "
        "VALUES ('engineering', 'Engineering', 'Selected roles', %s, 'prompt', 'hash', "
        "'gpt-5.6-luna', true, 1, now(), now()) RETURNING id",
        (user["id"],),
    )
    assert board is not None
    return board["id"]


def _job(board_id: int, index: int, sort_at: datetime.datetime) -> None:
    job = db.query_one(
        "INSERT INTO jobs "
        "(url, raw_url, company, title, locations, terms, source, active, date_posted, "
        "comp_min, comp_max, comp_currency, comp_period, comp_basis, comp_text) "
        "VALUES (%s, 'private-raw-url', %s, %s, ARRAY['New York'], ARRAY['secret-term'], "
        "'public-test', true, %s, 100000, 150000, 'USD', 'year', 'base', '$100k-$150k') "
        "RETURNING id",
        (f"https://example.com/jobs/{index}", f"Company {index}", f"Role {index}", sort_at),
    )
    assert job is not None
    db.execute(
        "INSERT INTO managed_board_jobs "
        "(managed_board_id, job_id, sort_at, projection_revision, resolved_model) "
        "VALUES (%s, %s, %s, 1, 'private-model')",
        (board_id, job["id"], sort_at),
    )


def test_public_index_requires_no_auth_and_does_not_provision(client):
    _published_board()
    before = db.query_one("SELECT count(*) AS n FROM users")
    assert before is not None

    response = client.get("/v1/public/job-lists")

    assert response.status_code == 200
    assert response.json()["job_lists"][0]["slug"] == "engineering"
    after = db.query_one("SELECT count(*) AS n FROM users")
    assert after is not None and after["n"] == before["n"]
    assert "401" not in response.text


def test_public_detail_allowlists_fields_and_paginates_stably(client):
    board_id = _published_board()
    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES ('public-test', 'https://example.com')"
    )
    instant = datetime.datetime(2026, 9, 11, 12, tzinfo=datetime.UTC)
    for index in range(1, 4):
        _job(board_id, index, instant)

    first = client.get("/v1/public/job-lists/engineering?limit=2")
    assert first.status_code == 200
    body = first.json()
    assert [job["title"] for job in body["jobs"]] == ["Role 3", "Role 2"]
    assert body["job_count"] == 3 and body["has_more"] is True
    assert set(body["jobs"][0]) == {
        "company",
        "title",
        "locations",
        "date_posted",
        "compensation",
        "url",
    }
    serialized = first.text
    for private_value in ("private-raw-url", "secret-term", "public-test", "private-model"):
        assert private_value not in serialized

    second = client.get(
        "/v1/public/job-lists/engineering", params={"limit": 2, "cursor": body["next_cursor"]}
    )
    assert second.status_code == 200
    assert [job["title"] for job in second.json()["jobs"]] == ["Role 1"]
    assert second.json()["next_cursor"] is None and second.json()["has_more"] is False


def test_unknown_and_unpublished_have_identical_refusals(client):
    board_id = _published_board()
    unknown = client.get("/v1/public/job-lists/missing")
    db.execute("UPDATE managed_boards SET published = false WHERE id = %s", (board_id,))
    unpublished = client.get("/v1/public/job-lists/engineering")

    assert unknown.status_code == unpublished.status_code == 404
    assert unknown.json() == unpublished.json() == {
        "detail": {"code": "NOT_FOUND", "message": "unknown job list"}
    }
    assert unknown.headers["cache-control"] == unpublished.headers["cache-control"] == "no-store"


def test_public_etags_revalidate(client):
    _published_board()
    first = client.get("/v1/public/job-lists")
    cached = client.get("/v1/public/job-lists", headers={"If-None-Match": first.headers["etag"]})

    assert first.headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert cached.status_code == 304 and cached.content == b""


def test_public_detail_rejects_malformed_cursor(client):
    _published_board()
    response = client.get("/v1/public/job-lists/engineering?cursor=not-a-cursor")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_CURSOR"
