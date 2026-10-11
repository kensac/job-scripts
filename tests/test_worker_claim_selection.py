"""Worker queue admission, claim order, and stale-claim recovery."""

from __future__ import annotations

import pytest

from api import db, worker
from tasks import runtime as tasks_runtime
from tests.factories import make_source
from tests.worker_queue_support import _clear_claim as _clear_claim

pytestmark = pytest.mark.usefixtures("_clear_claim")


def test_enqueue_dedupe_key_second_call_returns_none():
    first = tasks_runtime.enqueue("run_filter", {"x": 1}, dedupe_key="dk1")
    second = tasks_runtime.enqueue("run_filter", {"x": 2}, dedupe_key="dk1")
    assert first is not None
    assert second is None
    rows = db.query("SELECT id FROM tasks WHERE dedupe_key = %s", ("dk1",))
    assert len(rows) == 1


def test_claim_task_returns_none_on_empty_queue():
    assert worker._claim_task() is None


def test_claim_task_flips_to_running_and_stamps_worker():
    task_id = tasks_runtime.enqueue("run_filter", {"a": 1})
    claimed = worker._claim_task()
    assert claimed["id"] == task_id
    row = db.query_one(
        "SELECT status, attempts, worker, last_heartbeat FROM tasks WHERE id = %s",
        (task_id,),
    )
    assert row["status"] == "running"
    assert row["attempts"] == 1
    assert row["worker"] == worker.WORKER_NAME
    assert row["last_heartbeat"] is not None


def test_claim_task_claims_regular_tasks_in_enqueue_order():
    first_id = tasks_runtime.enqueue("run_filter", {"a": 1})
    second_id = tasks_runtime.enqueue("run_filter", {"a": 2})
    assert worker._claim_task()["id"] == first_id
    assert worker._claim_task()["id"] == second_id


def _pull(source: str, *, age_hours: float = 0) -> int:
    """A scheduled pull as the scheduler writes it: its payload names a host."""
    task_id = tasks_runtime.enqueue("ingest_source", {"source": source, "host": "pull.test"})
    db.execute(
        "UPDATE tasks SET created_at = now() - make_interval(secs => %s) WHERE id = %s",
        (age_hours * 3600, task_id),
    )
    return task_id


def test_claim_puts_a_backlog_of_pulls_behind_poll_batches():
    source = make_source()
    db.execute("UPDATE sources SET ingest_interval_hours = 24 WHERE name = %s", (source,))
    for _ in range(50):
        _pull(source, age_hours=0.5)
    poll_id = tasks_runtime.enqueue("poll_batches", {})
    assert worker._claim_task()["id"] == poll_id
    assert worker._claim_task()["kind"] == "ingest_source"


def test_claim_still_takes_a_pull_older_than_its_source_interval():
    source = make_source()
    db.execute("UPDATE sources SET ingest_interval_hours = 1 WHERE name = %s", (source,))
    aged_id = _pull(source, age_hours=2)
    tasks_runtime.enqueue("poll_batches", {})
    assert worker._claim_task()["id"] == aged_id


def test_claim_does_not_shift_a_pull_a_person_asked_for():
    """An admin pull carries no host, so it keeps its place by age."""
    manual_id = tasks_runtime.enqueue("ingest_source", {"source": make_source()})
    tasks_runtime.enqueue("poll_batches", {})
    assert worker._claim_task()["id"] == manual_id


def test_claim_task_leaves_a_kind_this_image_cannot_run_for_one_that_can():
    """A host still on the old image sees the kind a rolled host enqueued.
    gcp-vps claimed the first classify_locations task on 2026-09-05 and
    failed it as unknown, seconds before its own deploy."""
    unknown_id = tasks_runtime.enqueue("kind_from_a_newer_image", {})
    known_id = tasks_runtime.enqueue("run_filter", {"a": 1})
    assert worker._claim_task()["id"] == known_id
    assert worker._claim_task() is None
    row = db.query_one("SELECT status, attempts FROM tasks WHERE id = %s", (unknown_id,))
    assert (row["status"], row["attempts"]) == ("pending", 0)


def test_reap_stale_tasks_requeues_below_max_attempts():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    db.execute(
        "UPDATE tasks SET last_heartbeat = now() - interval '20 minutes' WHERE id = %s",
        (task_id,),
    )
    worker.reap_stale_tasks()
    row = db.query_one(
        "SELECT status, attempts, started_at, last_heartbeat FROM tasks WHERE id = %s",
        (task_id,),
    )
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["started_at"] is None
    assert row["last_heartbeat"] is None


def test_reap_stale_tasks_fails_after_max_attempts():
    task_id = tasks_runtime.enqueue("run_filter", {})
    db.execute(
        "UPDATE tasks SET status = 'running', attempts = 3, "
        "last_heartbeat = now() - interval '20 minutes' WHERE id = %s",
        (task_id,),
    )
    worker.reap_stale_tasks()
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "failed"
    assert "worker lost" in row["error"]


def test_reaper_does_not_count_batch_resumes_as_attempts():
    """Five claims, three of them resumes of a parked batch wait: two real
    attempts spent, so a lost worker is requeued. Task 5690708 was failed
    here at three claims, at least one of them the resume that collected its
    batches."""
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    db.execute(
        "UPDATE tasks SET status = 'running', attempts = 5, batch_resumes = 3, "
        "last_heartbeat = now() - interval '20 minutes' WHERE id = %s",
        (task_id,),
    )
    worker.reap_stale_tasks()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "pending"


def test_reaper_failure_keeps_uncollected_batch_ids():
    """Three real attempts spent: the task fails, but the batches it names
    were never collected, so the ids that point at them stay."""
    task_id = tasks_runtime.enqueue(
        "run_filter_batch_chunk", {"parent_id": 1, "batch_ids": ["batch_paid"]}
    )
    db.execute(
        "UPDATE tasks SET status = 'running', attempts = 5, batch_resumes = 2, "
        "last_heartbeat = now() - interval '20 minutes' WHERE id = %s",
        (task_id,),
    )
    worker.reap_stale_tasks()
    row = db.query_one("SELECT status, error FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "failed"
    assert "after 3 attempts" in row["error"]
    assert tasks_runtime.pending_batch_ids(task_id) == ["batch_paid"]


def test_reap_stale_tasks_leaves_fresh_heartbeat_running():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    worker.reap_stale_tasks()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running"
