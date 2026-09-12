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


def _job(
    board_id: int,
    index: int,
    sort_at: datetime.datetime,
    *,
    company: str | None = None,
    title: str | None = None,
    terms: list[str] | None = None,
    comp_max: int | None = 150000,
    date_posted: datetime.datetime | None = None,
) -> int:
    job = db.query_one(
        "INSERT INTO jobs "
        "(url, raw_url, company, title, locations, terms, source, active, date_posted, "
        "comp_min, comp_max, comp_currency, comp_period, comp_basis, comp_text, created_at) "
        "VALUES (%s, 'private-raw-url', %s, %s, ARRAY['New York'], %s, "
        "'public-test', true, %s, 100000, %s, 'USD', 'year', 'base', '$100k-$150k', %s) "
        "RETURNING id",
        (
            f"https://boards.greenhouse.io/example/jobs/{index}",
            company or f"Company {index}",
            title or f"Role {index}",
            terms or ["full-time"],
            date_posted if date_posted is not None else sort_at,
            comp_max,
            sort_at,
        ),
    )
    assert job is not None
    db.execute(
        "INSERT INTO managed_board_jobs "
        "(managed_board_id, job_id, sort_at, projection_revision, resolved_model) "
        "VALUES (%s, %s, %s, 1, 'private-model')",
        (board_id, job["id"], sort_at),
    )
    return job["id"]


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
        "job_id",
        "company",
        "title",
        "locations",
        "terms",
        "source",
        "ats",
        "date_posted",
        "added_at",
        "active",
        "closed_verdict",
        "compensation",
        "url",
    }
    serialized = first.text
    assert body["jobs"][0]["terms"] == ["full-time"]
    assert body["jobs"][0]["source"] == "public-test"
    assert body["jobs"][0]["ats"] == "greenhouse"
    for private_value in ("private-raw-url", "private-model"):
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
    assert (
        unknown.json()
        == unpublished.json()
        == {"detail": {"code": "NOT_FOUND", "message": "unknown job list"}}
    )
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


def test_public_list_filters_before_keyset_pagination(client):
    board_id = _published_board()
    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES ('public-test', 'https://example.com')"
    )
    instant = datetime.datetime(2026, 9, 11, 12, tzinfo=datetime.UTC)
    _job(board_id, 1, instant, company="Beta", title="Designer", terms=["internship"])
    _job(board_id, 2, instant, company="Alpha", title="Engineer", terms=["full-time"])
    _job(board_id, 3, instant, company="Alpha", title="Engineering Intern", terms=["internship"])

    first = client.get(
        "/v1/public/job-lists/engineering",
        params={
            "sort": "company",
            "dir": "asc",
            "q": "engineer",
            "terms": "internship",
            "limit": 1,
        },
    )
    assert first.status_code == 200
    body = first.json()
    assert [row["title"] for row in body["jobs"]] == ["Engineering Intern"]
    assert body["has_more"] is False


def test_each_public_sort_has_stable_keyset_pagination(client):
    board_id = _published_board()
    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES ('public-test', 'https://example.com')"
    )
    instant = datetime.datetime(2026, 9, 11, 12, tzinfo=datetime.UTC)
    for index in range(1, 5):
        _job(
            board_id,
            index,
            instant + datetime.timedelta(minutes=index),
            company="Same",
            title="Same",
            comp_max=100000,
            date_posted=instant,
        )

    for sort in ("posted", "added", "company", "title", "comp"):
        seen: list[int] = []
        cursor = None
        while True:
            response = client.get(
                "/v1/public/job-lists/engineering",
                params={
                    "sort": sort,
                    "dir": "desc",
                    "limit": 2,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            assert response.status_code == 200
            body = response.json()
            seen.extend(row["job_id"] for row in body["jobs"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 4


def test_cursor_is_bound_to_sort_and_direction(client):
    board_id = _published_board()
    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES ('public-test', 'https://example.com')"
    )
    instant = datetime.datetime(2026, 9, 11, 12, tzinfo=datetime.UTC)
    _job(board_id, 1, instant)
    _job(board_id, 2, instant)
    first = client.get("/v1/public/job-lists/engineering", params={"limit": 1})
    cursor = first.json()["next_cursor"]

    wrong_sort = client.get(
        "/v1/public/job-lists/engineering", params={"sort": "company", "cursor": cursor}
    )
    wrong_direction = client.get(
        "/v1/public/job-lists/engineering", params={"dir": "asc", "cursor": cursor}
    )
    assert wrong_sort.status_code == wrong_direction.status_code == 400


def test_public_job_detail_is_projection_scoped_and_exposes_cached_content(client):
    board_id = _published_board()
    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES ('public-test', 'https://example.com')"
    )
    instant = datetime.datetime(2026, 9, 11, 12, tzinfo=datetime.UTC)
    included = _job(board_id, 1, instant)
    excluded = _job(board_id, 2, instant)
    db.execute(
        "DELETE FROM managed_board_jobs WHERE managed_board_id = %s AND job_id = %s",
        (board_id, excluded),
    )
    url = db.query_one("SELECT url FROM jobs WHERE id = %s", (included,))["url"]
    db.execute(
        "INSERT INTO ai_queries (url, check_type, status, input_content, created_at) "
        "VALUES (%s, 'content', 'passed', 'public description', %s), "
        "(%s, 'closed', 'rejected', NULL, %s)",
        (url, instant, url, instant),
    )

    detail = client.get(f"/v1/public/job-lists/engineering/jobs/{included}")
    missing = client.get(f"/v1/public/job-lists/engineering/jobs/{excluded}")
    assert detail.status_code == 200
    assert detail.json()["job"]["job_id"] == included
    assert detail.json()["job"]["closed_verdict"] == "closed"
    assert detail.json()["content"] == "public description"
    assert detail.json()["content_fetched_at"] == instant.isoformat().replace("+00:00", "Z")
    assert missing.status_code == 404
    for private_field in ("status", "date_applied", "notes", "recruiter", "documents", "hidden"):
        assert private_field not in detail.text
