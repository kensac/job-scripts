from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from psycopg.errors import UniqueViolation

from api import db


@pytest.fixture(autouse=True)
def _available_owner_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")


def test_database_refuses_multiple_enabled_filters(f):
    uid = f.make_user()
    f.make_filter(uid, name="first", enabled=True)
    with pytest.raises(UniqueViolation):
        f.make_filter(uid, name="second", enabled=True)


@pytest.mark.parametrize("status", ["pending", "running", "waiting", "awaiting_batch"])
@pytest.mark.parametrize("admin", [False, True])
def test_run_all_refuses_existing_individual(
    client, user_headers, admin_headers, f, status, admin, runs_permitted
):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, enabled=True)
    task = db.query_one(
        "INSERT INTO tasks (kind, payload, status) VALUES ('run_filter', %s, %s) RETURNING id",
        (db.jsonb({"user_id": uid, "filter_id": flt["id"]}), status),
    )
    response = (
        client.post("/v1/admin/filters/run", json={"user_id": uid}, headers=admin_headers)
        if admin
        else client.post("/v1/user/filters/run-all", headers=user_headers)
    )
    assert response.status_code == 409
    assert response.json()["detail"]["task_id"] == task["id"]
    assert db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = 'run_all_filters'")["n"] == 0


def test_concurrent_filter_creation_has_one_winner(client, user_headers):
    ready = Barrier(2)

    def create(name):
        ready.wait(timeout=10)
        return client.post(
            "/v1/user/filters",
            json={"name": name, "prompt": "remote", "enabled": True},
            headers=user_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(create, ["one", "two"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    refusal = next(r for r in responses if r.status_code == 409)
    assert refusal.json()["detail"]["code"] == "ONE_FILTER"
    assert db.query_one("SELECT count(*) AS n FROM user_filters WHERE enabled")["n"] == 1


def test_scheduler_refuses_pending_individual_run(f):
    from tasks.ingest import schedule_filter_runs

    uid = f.make_user(groups=["jobtracker-users-internal"])
    f.subscribe(uid, "test-source")
    flt = f.make_filter(uid, enabled=True)
    db.execute(
        "INSERT INTO tasks (kind, payload) VALUES ('run_filter', %s)",
        (db.jsonb({"user_id": uid, "filter_id": flt["id"]}),),
    )
    schedule_filter_runs("integrity")
    assert db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = 'run_all_filters'")["n"] == 0


def test_save_defers_when_an_overlapping_run_exists(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, enabled=True)
    db.execute(
        "UPDATE app_config SET value = %s WHERE key = %s",
        (db.jsonb(["*"]), "filter_rejudge_on_change_groups"),
    )
    db.execute(
        "INSERT INTO tasks (kind, payload) VALUES ('run_all_filters', %s)",
        (db.jsonb({"user_id": uid}),),
    )
    response = client.patch(
        f"/v1/user/filters/{flt['id']}", json={"prompt": "a new prompt"}, headers=user_headers
    )
    assert response.status_code == 200
    assert response.json()["run_blocked"] == "DEFERRED"
    assert response.json()["task_id"] is None
    assert db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = 'run_filter'")["n"] == 0


def test_filter_list_reports_run_all_admission(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, enabled=True)
    task = db.query_one(
        "INSERT INTO tasks (kind, payload) VALUES ('run_filter', %s) RETURNING id",
        (db.jsonb({"user_id": uid, "filter_id": flt["id"]}),),
    )
    body = client.get("/v1/user/filters", headers=user_headers).json()
    assert body["run_all_task"] is None
    assert body["run_all_admission"]["allowed"] is False
    assert body["run_all_admission"]["task_id"] == task["id"]
    assert body["filters"][0]["run_admission"]["allowed"] is False


def test_concurrent_run_all_requests_have_one_winner(client, user_headers, runs_permitted):
    ready = Barrier(2)

    def run(_):
        ready.wait(timeout=10)
        return client.post("/v1/user/filters/run-all", headers=user_headers)

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(run, range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = 'run_all_filters'")["n"] == 1


def test_concurrent_enable_and_adopt_have_one_winner(client, user_headers, admin_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, enabled=False)
    preset = client.post(
        "/v1/admin/filter-presets",
        json={"name": "new preset", "prompt": "remote"},
        headers=admin_headers,
    ).json()
    ready = Barrier(2)

    def save(kind):
        ready.wait(timeout=10)
        if kind == "enable":
            return client.patch(
                f"/v1/user/filters/{flt['id']}", json={"enabled": True}, headers=user_headers
            )
        return client.post(f"/v1/filter-presets/{preset['id']}/adopt", headers=user_headers)

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(save, ["enable", "adopt"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert (
        next(r for r in responses if r.status_code == 409).json()["detail"]["code"] == "ONE_FILTER"
    )
    assert db.query_one("SELECT count(*) AS n FROM user_filters WHERE enabled")["n"] == 1


def test_migration_refuses_ambiguous_choices_without_changing_them(f):
    import importlib.util
    import os
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    uid = f.make_user()
    first = f.make_filter(uid, enabled=True)
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/3ffc161e4389_enforce_one_enabled_filter_per_user.py"
    )
    spec = importlib.util.spec_from_file_location("filter_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine(
        os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://")
    )
    try:
        with engine.connect() as connection:
            outer = connection.begin()
            try:
                connection.execute(sa.text("DROP INDEX uq_user_filters_one_enabled"))
                connection.execute(
                    sa.text(
                        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash, enabled) "
                        "VALUES (:uid, 'second', 'remote', 'another-hash', true)"
                    ),
                    {"uid": uid},
                )
                with (
                    pytest.raises(sa.exc.DBAPIError, match="1 users have multiple enabled filters"),
                    connection.begin_nested(),
                    Operations.context(MigrationContext.configure(connection)),
                ):
                    module.upgrade()
                rows = (
                    connection.execute(
                        sa.text(
                            "SELECT name FROM user_filters WHERE user_id = :uid AND enabled ORDER BY name"
                        ),
                        {"uid": uid},
                    )
                    .scalars()
                    .all()
                )
                assert sorted(rows) == sorted([first["name"], "second"])
            finally:
                outer.rollback()
    finally:
        engine.dispose()


def test_repeating_enabled_filter_save_does_not_queue_another_judgement(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    flt = f.make_filter(uid, enabled=True)
    db.execute(
        "UPDATE app_config SET value = %s WHERE key = 'filter_rejudge_on_change_groups'",
        (db.jsonb(["*"]),),
    )
    response = client.patch(
        f"/v1/user/filters/{flt['id']}",
        json={"enabled": True, "prompt": flt["prompt"]},
        headers=user_headers,
    )
    assert response.status_code == 200
    assert response.json()["task_id"] is None
    assert response.json()["run_blocked"] is None
    assert response.json()["prompt_hash"] == flt["prompt_hash"]
    assert db.query_one("SELECT count(*) AS n FROM tasks WHERE kind = 'run_filter'")["n"] == 0
