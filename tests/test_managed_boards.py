from __future__ import annotations

import psycopg
import pytest

from api import db
from core.filters import compute_filter_hash


def _source(name: str) -> None:
    db.execute(
        "INSERT INTO sources (name, listings_url) VALUES (%s, %s)", (name, f"https://{name}")
    )


def _create(client, headers, **overrides):
    body = {
        "slug": "software-engineering",
        "name": "Software Engineering",
        "prompt": "Prefer backend roles",
        "requested_model": "gpt-5.6-luna",
        "sources": ["source-b", "source-a"],
        **overrides,
    }
    return client.post("/v1/admin/managed-boards", json=body, headers=headers)


def test_managed_board_create_get_and_list_are_typed_and_deterministic(client, admin_headers):
    _source("source-a")
    _source("source-b")

    response = _create(client, admin_headers, on_ambiguous="filter")
    assert response.status_code == 200, response.text
    board = response.json()
    sponsor = db.query_one("SELECT id FROM users WHERE sub = 'test-admin'")["id"]
    assert board == {
        "id": board["id"],
        "slug": "software-engineering",
        "name": "Software Engineering",
        "description": "",
        "sponsor_user_id": sponsor,
        "prompt": "Prefer backend roles",
        "prompt_hash": compute_filter_hash("Prefer backend roles", "filter"),
        "requested_model": "gpt-5.6-luna",
        "on_ambiguous": "filter",
        "fail_closed": False,
        "criteria": {
            "date_posted_after": None,
            "max_age_days": None,
            "excluded_locations": [],
            "included_locations": [],
        },
        "published": False,
        "revision": 1,
        "public_revision": None,
        "projection_updated_at": None,
        "published_at": None,
        "unpublished_at": None,
        "created_at": board["created_at"],
        "updated_at": board["updated_at"],
        "sources": ["source-a", "source-b"],
    }
    assert (
        client.get(f"/v1/admin/managed-boards/{board['id']}", headers=admin_headers).json() == board
    )
    assert client.get("/v1/admin/managed-boards", headers=admin_headers).json() == {
        "boards": [board]
    }


@pytest.mark.parametrize(
    ("overrides", "status", "code"),
    [
        ({"slug": "Not-Kebab"}, 422, None),
        ({"on_ambiguous": "reject"}, 400, "INVALID_ON_AMBIGUOUS"),
        ({"requested_model": "imaginary-model"}, 400, "UNKNOWN_MODEL"),
        ({"sources": ["source-a", "source-a"]}, 400, "DUPLICATE_SOURCE"),
        ({"sources": ["missing"]}, 400, "UNKNOWN_SOURCE"),
        ({"criteria": {"max_age_days": 0}}, 422, None),
    ],
)
def test_managed_board_create_refusals(client, admin_headers, overrides, status, code):
    _source("source-a")
    response = _create(client, admin_headers, **overrides)
    assert response.status_code == status
    if code:
        assert response.json()["detail"]["code"] == code
    assert db.query_one("SELECT count(*) AS n FROM managed_boards")["n"] == 0


def test_managed_board_duplicate_and_immutable_fields_are_refused(client, admin_headers):
    _source("source-a")
    _source("source-b")
    first = _create(client, admin_headers)
    assert first.status_code == 200
    duplicate = _create(client, admin_headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "DUPLICATE_SLUG"
    board_id = first.json()["id"]
    for field, value in (("slug", "changed"), ("sponsor_user_id", 999)):
        response = client.patch(
            f"/v1/admin/managed-boards/{board_id}",
            json={"expected_revision": 1, field: value},
            headers=admin_headers,
        )
        assert response.status_code == 422


def test_managed_board_patch_is_atomic_revisioned_and_publish_is_explicit(client, admin_headers):
    _source("source-a")
    _source("source-b")
    board = _create(client, admin_headers).json()
    changed = client.patch(
        f"/v1/admin/managed-boards/{board['id']}",
        json={
            "expected_revision": 1,
            "prompt": "Only platform roles",
            "on_ambiguous": "filter",
            "sources": ["source-a"],
            "published": True,
        },
        headers=admin_headers,
    )
    assert changed.status_code == 200, changed.text
    current = changed.json()
    assert current["revision"] == 2
    assert current["public_revision"] == 1
    assert current["published"] is True and current["published_at"] is not None
    assert current["sources"] == ["source-a"]
    assert current["prompt_hash"] == compute_filter_hash("Only platform roles", "filter")

    stale = client.patch(
        f"/v1/admin/managed-boards/{board['id']}",
        json={"expected_revision": 1, "name": "Lost update"},
        headers=admin_headers,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_revision"] == 2
    assert (
        db.query_one("SELECT name FROM managed_boards WHERE id = %s", (board["id"],))["name"]
        == "Software Engineering"
    )

    republished_config = client.patch(
        f"/v1/admin/managed-boards/{board['id']}",
        json={"expected_revision": 2, "name": "Platform Engineering"},
        headers=admin_headers,
    ).json()
    assert republished_config["revision"] == 3
    assert republished_config["public_revision"] == 2

    unpublished = client.patch(
        f"/v1/admin/managed-boards/{board['id']}",
        json={"expected_revision": 3, "published": False},
        headers=admin_headers,
    ).json()
    assert unpublished["revision"] == 4
    assert unpublished["published"] is False
    assert unpublished["public_revision"] == 3
    assert unpublished["published_at"] == current["published_at"]
    assert unpublished["unpublished_at"] is not None

    republished = client.patch(
        f"/v1/admin/managed-boards/{board['id']}",
        json={"expected_revision": 4, "published": True},
        headers=admin_headers,
    ).json()
    assert republished["revision"] == 5 and republished["public_revision"] == 4
    assert republished["published_at"] is not None and republished["unpublished_at"] is None

    db.execute(
        "UPDATE managed_boards SET public_revision = public_revision + 1, projection_updated_at = now() WHERE id = %s",
        (board["id"],),
    )
    projected = client.get(f"/v1/admin/managed-boards/{board['id']}", headers=admin_headers).json()
    assert projected["revision"] == 5 and projected["public_revision"] == 5


def test_publishing_without_sources_rolls_back_revision(client, admin_headers):
    response = _create(client, admin_headers, sources=[])
    board = response.json()
    failed = client.patch(
        f"/v1/admin/managed-boards/{board['id']}",
        json={"expected_revision": 1, "name": "Should roll back", "published": True},
        headers=admin_headers,
    )
    assert failed.status_code == 400
    assert failed.json()["detail"]["code"] == "NO_SOURCES"
    assert db.query_one(
        "SELECT name, revision FROM managed_boards WHERE id = %s", (board["id"],)
    ) == {"name": "Software Engineering", "revision": 1}


def test_api_usage_subject_constraint_accepts_fleet_user_or_board_not_both(client, admin_headers):
    board = _create(client, admin_headers, sources=[]).json()
    user_id = board["sponsor_user_id"]
    base = "INSERT INTO api_usage (user_id, managed_board_id, key_source, purpose) VALUES (%s, %s, 'fleet', 'test')"
    db.execute(base, (None, None))
    db.execute(base, (user_id, None))
    db.execute(base, (None, board["id"]))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(base, (user_id, board["id"]))


def test_source_delete_reports_and_force_removes_managed_board_membership(client, admin_headers):
    _source("source-a")
    board = _create(client, admin_headers, sources=["source-a"]).json()
    refused = client.delete("/v1/admin/sources/source-a", headers=admin_headers)
    assert refused.status_code == 409
    assert refused.json()["detail"]["attached"]["managed_boards"] == 1
    deleted = client.delete("/v1/admin/sources/source-a?force=true", headers=admin_headers)
    assert deleted.status_code == 200
    assert deleted.json()["was_attached"]["managed_boards"] == 1
    assert db.query_one(
        "SELECT b.revision, count(s.source) AS sources FROM managed_boards b "
        "LEFT JOIN managed_board_sources s ON s.managed_board_id = b.id "
        "WHERE b.id = %s GROUP BY b.id",
        (board["id"],),
    ) == {"revision": 2, "sources": 0}


def test_bootstrap_creates_exactly_two_draft_boards_and_is_idempotent(client, admin_headers):
    _source("active-a")
    _source("active-b")
    db.execute("UPDATE sources SET active = false WHERE name = 'active-b'")

    first = client.post("/v1/admin/managed-boards/bootstrap", headers=admin_headers)
    assert first.status_code == 200, first.text
    boards = first.json()["boards"]
    assert [board["slug"] for board in boards] == [
        "software-engineering-internships",
        "software-engineering-new-grad",
    ]
    assert [board["name"] for board in boards] == [
        "Selective Tech Internships",
        "Selective Tech New Grad",
    ]
    assert [board["description"] for board in boards] == [
        "Prestigious software engineering and product management internships at tech and tech-adjacent companies.",
        "Prestigious full-time software engineering and product management opportunities for new graduates at tech and tech-adjacent companies.",
    ]
    for board in boards:
        assert board["requested_model"] == "gpt-5.6-luna"
        assert board["on_ambiguous"] == "filter"
        assert board["fail_closed"] is True
        assert board["criteria"] == {
            "date_posted_after": None,
            "max_age_days": 7,
            "excluded_locations": [],
            "included_locations": ["United States", "Canada", "Remote"],
        }
        assert board["sources"] == ["active-a"]
        assert board["published"] is False and board["revision"] == 1
    for board in boards:
        prompt = board["prompt"].lower()
        assert "software engineering or product management" in prompt
        assert "prestigious company tier" in prompt
        assert "selective and top-tier" in prompt
        assert "unclear" in prompt
    assert "internship" in boards[0]["prompt"].lower()
    assert "full-time" in boards[1]["prompt"].lower()

    repeated = client.post("/v1/admin/managed-boards/bootstrap", headers=admin_headers)
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert db.query_one("SELECT count(*) AS n FROM managed_boards")["n"] == 2
    assert db.query_one("SELECT count(*) AS n FROM users")["n"] == 1
    assert db.query_one("SELECT count(*) AS n FROM user_jobs")["n"] == 0


def test_bootstrap_drift_refuses_and_rolls_back_both_boards(client, admin_headers):
    first = client.post("/v1/admin/managed-boards/bootstrap", headers=admin_headers)
    assert first.status_code == 200
    ids = [board["id"] for board in first.json()["boards"]]
    db.execute("DELETE FROM managed_boards WHERE id = %s", (ids[0],))
    db.execute("UPDATE managed_boards SET prompt = 'drift' WHERE id = %s", (ids[1],))

    response = client.post("/v1/admin/managed-boards/bootstrap", headers=admin_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "BOOTSTRAP_DRIFT"
    assert db.query_one("SELECT count(*) AS n FROM managed_boards")["n"] == 1
    assert (
        db.query_one("SELECT prompt FROM managed_boards WHERE id = %s", (ids[1],))["prompt"]
        == "drift"
    )
