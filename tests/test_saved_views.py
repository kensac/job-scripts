"""A view is a name that returns a page to a state; several per page, one
default, ordered, and only ever the caller's own."""

from __future__ import annotations

import pytest
from psycopg.errors import UniqueViolation

from api import db


def test_views_are_per_page_ordered_with_one_default(client, user_headers, other_user_headers):
    state = {
        "filters": {"status": ["pending", "running"]},
        "sorts": [{"key": "kind", "dir": "asc"}],
    }
    r = client.post(
        "/v1/user/views",
        json={"page": "admin.queue", "name": "Live work", "state": state, "is_default": True},
        headers=user_headers,
    )
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["is_default"] is True and first["position"] == 0 and first["state"] == state
    second = client.post(
        "/v1/user/views",
        json={
            "page": "admin.queue",
            "name": "Failures",
            "state": {"filters": {"status": ["failed"]}},
        },
        headers=user_headers,
    ).json()
    assert second["position"] == 1 and second["is_default"] is False
    board = client.post(
        "/v1/user/views",
        json={
            "page": "board",
            "name": "US only",
            "state": {"filters": {"statuses": ["not_applied"]}},
        },
        headers=user_headers,
    ).json()

    queue = client.get(
        "/v1/user/views", params={"page": "admin.queue"}, headers=user_headers
    ).json()
    assert [v["name"] for v in queue["views"]] == ["Live work", "Failures"]
    everything = client.get("/v1/user/views", headers=user_headers).json()
    assert {v["page"] for v in everything["views"]} == {"admin.queue", "board"}

    made_default = client.patch(
        f"/v1/user/views/{second['id']}", json={"is_default": True}, headers=user_headers
    ).json()
    assert made_default["is_default"] is True
    queue = client.get(
        "/v1/user/views", params={"page": "admin.queue"}, headers=user_headers
    ).json()
    assert [v["is_default"] for v in queue["views"]] == [False, True]

    dup = client.post(
        "/v1/user/views",
        json={"page": "admin.queue", "name": "Failures", "state": {}},
        headers=user_headers,
    )
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "DUPLICATE_NAME"

    renamed = client.patch(
        f"/v1/user/views/{board['id']}",
        json={
            "name": "US, not applied",
            "state": {
                "filters": {"statuses": ["not_applied"]},
                "sorts": [{"key": "date_posted", "dir": "desc"}],
            },
        },
        headers=user_headers,
    ).json()
    assert (
        renamed["name"] == "US, not applied"
        and renamed["state"]["sorts"][0]["key"] == "date_posted"
    )

    theirs = client.get("/v1/user/views", headers=other_user_headers).json()
    assert theirs["views"] == []
    assert (
        client.patch(
            f"/v1/user/views/{board['id']}", json={"name": "x"}, headers=other_user_headers
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/v1/user/views/{board['id']}", headers=other_user_headers).status_code
        == 404
    )

    assert client.delete(f"/v1/user/views/{board['id']}", headers=user_headers).json() == {
        "deleted": board["id"]
    }
    assert (
        client.get("/v1/user/views", params={"page": "board"}, headers=user_headers).json()["views"]
        == []
    )

    too_big = client.post(
        "/v1/user/views",
        json={"page": "board", "name": "huge", "state": {"x": "y" * 40_000}},
        headers=user_headers,
    )
    assert too_big.status_code == 400 and too_big.json()["detail"]["code"] == "STATE_TOO_LARGE"


def _create(client, headers, name, **fields):
    response = client.post(
        "/v1/user/views", headers=headers, json={"page": "board", "name": name, **fields}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("operation", ["create", "patch"])
def test_failed_default_save_preserves_previous_default(client, user_headers, operation):
    original = _create(client, user_headers, "Original", is_default=True)
    candidate = _create(client, user_headers, "Candidate")
    if operation == "create":
        response = client.post(
            "/v1/user/views",
            headers=user_headers,
            json={"page": "board", "name": "Original", "is_default": True},
        )
    else:
        response = client.patch(
            f"/v1/user/views/{candidate['id']}",
            headers=user_headers,
            json={"name": "Original", "is_default": True},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DUPLICATE_NAME"
    rows = db.query("SELECT id FROM saved_views WHERE is_default")
    assert rows == [{"id": original["id"]}]


@pytest.mark.parametrize("field", ["name", "state", "is_default", "position"])
def test_patch_rejects_explicit_null(client, user_headers, field):
    original = _create(client, user_headers, "Original", is_default=True)
    response = client.patch(
        f"/v1/user/views/{original['id']}", headers=user_headers, json={field: None}
    )
    assert response.status_code == 422, response.text
    assert db.query_one("SELECT name, state, is_default, position FROM saved_views") == {
        "name": "Original",
        "state": {},
        "is_default": True,
        "position": 0,
    }


@pytest.mark.parametrize("operation", ["create", "patch"])
def test_whitespace_only_names_rejected(client, user_headers, operation):
    original = _create(client, user_headers, "Original")
    if operation == "create":
        response = client.post(
            "/v1/user/views", headers=user_headers, json={"page": "board", "name": " \t "}
        )
    else:
        response = client.patch(
            f"/v1/user/views/{original['id']}", headers=user_headers, json={"name": " \t "}
        )
    assert response.status_code == 422, response.text


def test_database_enforces_one_default_per_page(client, user_headers):
    _create(client, user_headers, "Original", is_default=True)
    candidate = _create(client, user_headers, "Candidate")
    with pytest.raises(UniqueViolation):
        db.execute("UPDATE saved_views SET is_default = true WHERE id = %s", (candidate["id"],))


@pytest.mark.parametrize("operation", ["create", "patch"])
def test_concurrent_default_selection_serializes_on_owner(client, user_headers, operation):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from api.auth import AuthedUser
    from api.routers.views import ViewCreate, ViewPatch, create_view, patch_view

    first = _create(client, user_headers, "First")
    second = _create(client, user_headers, "Second")
    owner = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    user = AuthedUser(
        id=owner["id"], sub="test-user", email="user@example.com", name="Test", groups=[]
    )

    def select_default(index):
        if operation == "create":
            return create_view(ViewCreate(page="board", name=f"New {index}", is_default=True), user)
        view = [first, second][index]
        return patch_view(view["id"], ViewPatch(is_default=True), user)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with db.transaction():
            db.query_one("SELECT id FROM users WHERE id = %s FOR NO KEY UPDATE", (user.id,))
            futures = [executor.submit(select_default, index) for index in range(2)]
            # Inspect actual PostgreSQL waits rather than relying on timing to
            # manufacture a race; the deadline bounds a broken implementation.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                db.execute("SELECT pg_stat_clear_snapshot()")
                waiting = db.query_one(
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' "
                    "AND query LIKE 'SELECT id FROM users%FOR NO KEY UPDATE'"
                )["n"]
                if waiting == 2 or any(future.done() for future in futures):
                    break
                time.sleep(0.01)
            assert waiting == 2
        results = [future.result(timeout=5) for future in futures]
    assert len(results) == 2
    defaults = db.query("SELECT id FROM saved_views WHERE is_default")
    assert len(defaults) == 1
    assert defaults[0]["id"] in {result["id"] for result in results}
    if operation == "create":
        assert sorted(result["position"] for result in results) == [2, 3]


def test_default_migration_refuses_ambiguous_existing_choices(client, user_headers):
    import importlib.util
    import os
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    original = _create(client, user_headers, "Original", is_default=True)
    candidate = _create(client, user_headers, "Candidate")
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/5a736c33a9cf_enforce_one_saved_view_default_per_page.py"
    )
    spec = importlib.util.spec_from_file_location("saved_view_default_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(
        os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://", 1)
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(sa.text("DROP INDEX uq_saved_views_user_page_default"))
                connection.execute(sa.text("UPDATE saved_views SET is_default = true"))
                with (
                    Operations.context(MigrationContext.configure(connection)),
                    pytest.raises(RuntimeError, match="1 user/page groups have multiple defaults"),
                ):
                    migration.upgrade()
                assert connection.execute(
                    sa.text("SELECT id FROM saved_views WHERE is_default ORDER BY id")
                ).scalars().all() == [original["id"], candidate["id"]]
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
    assert db.query("SELECT id FROM saved_views WHERE is_default") == [{"id": original["id"]}]
