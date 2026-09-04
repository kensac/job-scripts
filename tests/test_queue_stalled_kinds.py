"""A worker is not stalled by work it is configured to refuse.

queue_stalled compared every idle worker against every pending task. That was
correct while no worker filtered kinds. JOBTRACKER_WORKER_EXCLUDE_KINDS made
it false: gcp-vps refuses the nine browser kinds, ingest_source is usually the
bulk of the queue, so it would have been reported as stalled for doing exactly
what it was configured to do. A detector's first impression is its
credibility, and this one's would have been a false alarm on the newest host.
"""

import datetime

import pytest

from api import db, health


@pytest.fixture
def clean():
    db.execute("DELETE FROM worker_status WHERE name LIKE 'test-%'")
    db.execute("DELETE FROM tasks WHERE kind = 'test_kind_excluded'")
    yield
    db.execute("DELETE FROM worker_status WHERE name LIKE 'test-%'")
    db.execute("DELETE FROM tasks WHERE kind = 'test_kind_excluded'")


def _worker(name, kinds=None, excluded=None):
    db.execute(
        "INSERT INTO worker_status (name, started_at, current_task_id, last_seen, kinds, "
        "excluded_kinds) VALUES (%s, now(), NULL, now(), %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET kinds = EXCLUDED.kinds, "
        "excluded_kinds = EXCLUDED.excluded_kinds, last_seen = now(), current_task_id = NULL",
        (name, kinds or [], excluded or []),
    )


def _old_pending_task():
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=3)
    db.execute(
        "INSERT INTO tasks (kind, payload, status, created_at) "
        "VALUES ('test_kind_excluded', '{}'::jsonb, 'pending', %s)",
        (old,),
    )


def _stalled_names():
    return {f["subject"] for f in health.detect() if f["kind"] == "queue_stalled"}


def test_a_worker_that_refuses_the_queued_kind_is_not_stalled(clean):
    _old_pending_task()
    _worker("test-excluder", excluded=["test_kind_excluded"])
    assert "test-excluder" not in _stalled_names()


def test_a_worker_that_could_claim_it_is_stalled(clean):
    _old_pending_task()
    _worker("test-claimer")
    assert "test-claimer" in _stalled_names()


def test_an_allowlist_that_omits_the_kind_is_not_stalled(clean):
    _old_pending_task()
    _worker("test-allowlisted", kinds=["something_else"])
    assert "test-allowlisted" not in _stalled_names()


def test_an_allowlist_that_includes_the_kind_is_stalled(clean):
    _old_pending_task()
    _worker("test-allowed", kinds=["test_kind_excluded"])
    assert "test-allowed" in _stalled_names()
