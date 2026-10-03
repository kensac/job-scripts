"""7ca95d34ef5f refuses to drop an inline value, and its downgrade restores the schema.

The migration commits between steps (VALIDATE and DROP INDEX CONCURRENTLY run
outside a transaction), so it cannot run inside a test's transaction. It runs
through alembic against a database of its own, created and dropped here.
"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from psycopg.rows import dict_row

from core.disposable_db import require_disposable_name

PARENT = "cd26607aae44"
HEAD = "7ca95d34ef5f"
ROOT = Path(__file__).parents[1]
INLINE = {
    "url",
    "prompt_hash",
    "stage",
    "mode",
    "action",
    "reason",
    "profile_id",
    "title",
    "content_hash",
    "policy_id",
    "policy",
    "evidence",
}
URL_KEYED = {"idx_review_gate_decisions_url_created", "uq_review_gate_decisions_task_url"}


@pytest.fixture
def scratch():
    dsn = os.environ["DATABASE_URL"]
    parsed = urlparse(dsn)
    name = require_disposable_name(parsed.path.lstrip("/").removesuffix("_test") + "_contract_test")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield urlunparse(parsed._replace(path=f"/{name}"))
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def alembic(url, *args):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=300,
    )


def query(url, sql, params=None):
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def columns(url):
    return {
        row["column_name"]: row["is_nullable"]
        for row in query(
            url,
            "SELECT column_name,is_nullable FROM information_schema.columns "
            "WHERE table_name='review_gate_decisions'",
        )
    }


def indexes(url):
    return {
        row["relname"]: row["indisvalid"]
        for row in query(
            url,
            "SELECT c.relname,i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE i.indrelid='review_gate_decisions'::regclass",
        )
    }


def constraints(url):
    return {
        row["conname"]: row["convalidated"]
        for row in query(
            url,
            "SELECT conname,convalidated FROM pg_constraint "
            "WHERE conrelid='review_gate_decisions'::regclass AND contype<>'n'",
        )
    }


def decisions(url):
    return query(url, "SELECT id,task_id,url_id,body_id,created_at FROM review_gate_decisions")


def version(url):
    return query(url, "SELECT version_num FROM alembic_version")[0]["version_num"]


def test_contract_refuses_inline_values_then_drops_and_downgrades(scratch):
    upgraded = alembic(scratch, "upgrade", PARENT)
    assert upgraded.returncode == 0, upgraded.stderr
    query(
        scratch,
        "INSERT INTO review_gate_policies(digest,policy) VALUES('p','{}');"
        "INSERT INTO review_gate_urls(url) VALUES('https://example.test/a'),"
        "('https://example.test/b');"
        "INSERT INTO review_gate_decision_bodies(digest,prompt_hash,stage,mode,action,title,"
        "policy_id,evidence) VALUES('b','h','detailed','off','review','Engineer',1,'{}');"
        "INSERT INTO review_gate_decisions(task_id,url_id,body_id) VALUES(1,1,1),(2,2,1),(3,1,1);"
        # One row still holds an inline copy of its title.
        "UPDATE review_gate_decisions SET title='Engineer' WHERE task_id=3",
    )
    stored = decisions(scratch)

    refused = alembic(scratch, "upgrade", HEAD)
    assert refused.returncode != 0
    assert "ck_review_gate_decisions_references_only" in refused.stderr
    assert version(scratch) == PARENT
    assert set(columns(scratch)) >= INLINE
    assert set(indexes(scratch)) >= URL_KEYED
    assert query(scratch, "SELECT title FROM review_gate_decisions WHERE task_id=3") == [
        {"title": "Engineer"}
    ]
    # Left behind NOT VALID by the refused run, it already refuses new inline
    # values; the rerun replaces it.
    assert constraints(scratch)["ck_review_gate_decisions_references_only"] is False

    query(scratch, "UPDATE review_gate_decisions SET title=NULL")
    applied = alembic(scratch, "upgrade", HEAD)
    assert applied.returncode == 0, applied.stderr
    assert version(scratch) == HEAD
    assert not INLINE & set(columns(scratch))
    assert columns(scratch)["url_id"] == columns(scratch)["body_id"] == "NO"
    assert not URL_KEYED & set(indexes(scratch))
    assert constraints(scratch) == {
        "review_gate_decisions_pkey": True,
        "fk_review_gate_decisions_url": True,
        "fk_review_gate_decisions_body": True,
    }
    assert decisions(scratch) == stored

    downgraded = alembic(scratch, "downgrade", PARENT)
    assert downgraded.returncode == 0, downgraded.stderr
    assert version(scratch) == PARENT
    assert set(columns(scratch)) >= INLINE
    assert columns(scratch)["url_id"] == columns(scratch)["body_id"] == "YES"
    assert all(indexes(scratch)[name] for name in URL_KEYED)
    assert {
        "uq_review_gate_decisions_task_url",
        "ck_review_gate_decisions_shape",
        "ck_review_gate_decisions_action",
        "fk_review_gate_decisions_policy",
    } <= set(constraints(scratch))
    assert decisions(scratch) == stored

    again = alembic(scratch, "upgrade", HEAD)
    assert again.returncode == 0, again.stderr
    assert decisions(scratch) == stored
