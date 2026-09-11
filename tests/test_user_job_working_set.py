from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from api import db
from api.orm import Base, UserJob, UserJobWorkingSet


def test_working_set_model_is_a_narrow_idempotent_association():
    table = UserJobWorkingSet.__table__

    assert Base.metadata.tables["user_job_working_set"] is table
    assert list(table.columns) == [table.c.user_id, table.c.job_id]
    assert [column.name for column in table.primary_key.columns] == ["user_id", "job_id"]
    assert {index.name for index in table.indexes} == {"idx_user_job_working_set_job"}
    assert {column.name for column in next(iter(table.indexes)).columns} == {"job_id"}
    assert {
        (foreign_key.parent.name, foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in table.foreign_keys
    } == {
        ("user_id", "users.id", "CASCADE"),
        ("job_id", "jobs.id", "CASCADE"),
    }
    assert UserJob.__table__.c.person_touched_at.nullable is True


def test_working_set_is_independent_of_person_state_and_cascades(f):
    user_id = f.make_user()
    job_id = f.make_job()

    db.execute(
        "INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, job_id),
    )
    db.execute(
        "INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, job_id),
    )

    assert db.query_one("SELECT count(*) AS n FROM user_job_working_set")["n"] == 1
    assert (
        db.query_one(
            "SELECT 1 FROM user_jobs WHERE user_id = %s AND job_id = %s", (user_id, job_id)
        )
        is None
    )

    db.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
    assert db.query_one("SELECT 1 FROM user_job_working_set") is None


def test_automated_materialization_does_not_mark_person_state(f):
    from tasks.board import materialize_passing

    user_id = f.make_user()
    source = f.make_source()
    f.subscribe(user_id, source)
    user_filter = f.make_filter(user_id)
    job_id, url = f.make_ready_job(source=source)
    f.make_verdict(url, "custom", prompt_hash=user_filter["prompt_hash"])

    assert materialize_passing(user_id) == 1
    assert (
        db.query_one(
            "SELECT person_touched_at FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )["person_touched_at"]
        is None
    )


def test_an_accepted_noop_person_patch_marks_an_existing_legacy_row(client, user_headers, f):
    user_id = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    job_id = f.make_job()
    db.execute("INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s)", (user_id, job_id))
    assert (
        db.query_one(
            "SELECT person_touched_at FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )["person_touched_at"]
        is None
    )

    response = client.patch(f"/v1/user/jobs/{job_id}", json={"hidden": False}, headers=user_headers)

    assert response.status_code == 200, response.text
    assert (
        db.query_one(
            "SELECT person_touched_at FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )["person_touched_at"]
        is not None
    )


def test_working_set_migration_downgrades_and_recreates_the_table():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/f4597fad7cea_add_user_job_working_set_foundation.py"
    )
    spec = importlib.util.spec_from_file_location("user_job_working_set_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(
        os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://", 1)
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Operations.context(MigrationContext.configure(connection)):
                    migration.downgrade()
                assert (
                    connection.scalar(sa.text("SELECT to_regclass('user_job_working_set')")) is None
                )
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM information_schema.columns "
                            "WHERE table_name = 'user_jobs' AND column_name = 'person_touched_at'"
                        )
                    )
                    == 0
                )

                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                assert (
                    connection.scalar(sa.text("SELECT to_regclass('user_job_working_set')"))
                    == "user_job_working_set"
                )
                assert (
                    connection.scalar(sa.text("SELECT to_regclass('idx_user_job_working_set_job')"))
                    == "idx_user_job_working_set_job"
                )
                touched = (
                    connection.execute(
                        sa.text(
                            "SELECT is_nullable, column_default FROM information_schema.columns "
                            "WHERE table_name = 'user_jobs' AND column_name = 'person_touched_at'"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert touched == {"is_nullable": "YES", "column_default": None}
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
