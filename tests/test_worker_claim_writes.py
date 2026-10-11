"""Claim-guarded task lifecycle writes and release behavior."""

from __future__ import annotations

import pytest

from api import db, worker
from tasks import runtime as tasks_runtime
from tests.worker_queue_support import _clear_claim as _clear_claim
from tests.worker_queue_support import (
    _hold_claim,
    _reclaim_elsewhere,
)

_REQUEUE_SQL = worker._REQUEUE_ON_EXIT_SQL


pytestmark = pytest.mark.usefixtures("_clear_claim")


def test_finish_marks_running_task_done():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    tasks_runtime.finish(task_id, "done")
    row = db.query_one("SELECT status, finished_at FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"
    assert row["finished_at"] is not None


def test_finish_preserves_cancelled_task():
    task_id = tasks_runtime.enqueue("run_filter", {})
    db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = %s", (task_id,))
    tasks_runtime.finish(task_id, "done")
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "cancelled"


def test_graceful_exit_requeue_sql_decrements_attempts_on_running_task():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    db.execute(_REQUEUE_SQL, (task_id, worker.WORKER_NAME, 1))
    row = db.query_one(
        "SELECT status, attempts, started_at, last_heartbeat FROM tasks WHERE id = %s",
        (task_id,),
    )
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["started_at"] is None
    assert row["last_heartbeat"] is None


def test_graceful_exit_requeue_sql_noop_on_done_task():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    tasks_runtime.finish(task_id, "done")
    db.execute(_REQUEUE_SQL, (task_id, worker.WORKER_NAME, 1))
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"


def test_graceful_exit_requeue_sql_noop_once_the_claim_is_gone():
    """A deploy is when a worker is most likely to have already lost its task:
    SIGTERM must not requeue a run another worker is midway through."""
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    _reclaim_elsewhere(task_id)
    db.execute(_REQUEUE_SQL, (task_id, worker.WORKER_NAME, 1))
    row = db.query_one("SELECT status, attempts, worker FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running"
    assert row["attempts"] == 2
    assert row["worker"] == "other-host"


def test_park_refuses_once_the_task_has_been_reclaimed():
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)
    _reclaim_elsewhere(task_id)

    assert tasks_runtime.park_awaiting_batch(task_id, ["batch_lost"]) is False
    row = db.query_one("SELECT status, worker FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running", "must not park a run another worker holds"
    assert row["worker"] == "other-host"
    # The ids are still recorded: they are paid work, and dropping them would
    # leave nothing pointing at the batches.
    assert tasks_runtime.pending_batch_ids(task_id) == ["batch_lost"]


def test_park_succeeds_while_the_claim_is_held():
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)

    assert tasks_runtime.park_awaiting_batch(task_id, ["batch_a"]) is True
    row = db.query_one(
        "SELECT status, started_at, last_heartbeat FROM tasks WHERE id = %s", (task_id,)
    )
    assert row["status"] == "awaiting_batch"
    assert row["started_at"] is None and row["last_heartbeat"] is None


def test_park_does_not_duplicate_ids_the_hook_already_recorded():
    """A two-wave submit records each id through the hook, then parks with the
    full list; collection walks that list, so a duplicate is a second download
    of the same output file."""
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)
    hook = tasks_runtime.batch_event_hook(task_id, "filter", "gpt-5-nano")
    hook("batch_1", "validating", {"requests": 1, "completed": 0, "failed": 0})
    hook("batch_2", "validating", {"requests": 1, "completed": 0, "failed": 0})

    assert tasks_runtime.park_awaiting_batch(task_id, ["batch_1", "batch_2"]) is True
    assert tasks_runtime.pending_batch_ids(task_id) == ["batch_1", "batch_2"]


def test_park_records_ids_the_hook_never_saw():
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)

    assert tasks_runtime.park_awaiting_batch(task_id, ["batch_1", "batch_2"]) is True
    assert tasks_runtime.pending_batch_ids(task_id) == ["batch_1", "batch_2"]


def test_finish_refuses_once_the_task_has_been_reclaimed():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    _hold_claim(task_id)
    _reclaim_elsewhere(task_id)

    tasks_runtime.finish(task_id, "done")
    row = db.query_one("SELECT status, finished_at FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running"
    assert row["finished_at"] is None


def test_finish_clears_batch_ids_so_a_rerun_cannot_recollect():
    """Verdicts are derived from scraped text captured before the batch was
    submitted. A finished task that kept its ids would collect them again on
    any later re-run and write verdicts from text that may predate a closure.
    """
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)
    hook = tasks_runtime.batch_event_hook(task_id, "filter", "gpt-5-nano")
    hook("batch_spent", "completed", {"requests": 1, "completed": 1, "failed": 0})
    assert tasks_runtime.pending_batch_ids(task_id) == ["batch_spent"]

    tasks_runtime.finish(task_id, "done")
    assert tasks_runtime.pending_batch_ids(task_id) == []
    row = db.query_one("SELECT status, payload FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done"
    assert row["payload"]["parent_id"] == 1, "only batch_ids is dropped"


def test_failed_task_keeps_batch_ids_it_never_collected():
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)
    hook = tasks_runtime.batch_event_hook(task_id, "filter", "gpt-5-nano")
    hook("batch_paid", "in_progress", {"requests": 1, "completed": 0, "failed": 0})

    tasks_runtime.finish(task_id, "failed", "boom")
    assert tasks_runtime.pending_batch_ids(task_id) == ["batch_paid"]


@pytest.mark.asyncio
async def test_batch_resumes_do_not_spend_the_transient_retries(monkeypatch):
    """A batch task parks and is resumed more times than task_max_attempts, then
    hits a transient error. It must still get its full retry budget: task
    5472607 was failed on its first full-disk error because each resume had
    been counted as an attempt."""
    max_attempts = db.get_config("task_max_attempts")
    resumes = max_attempts + 2
    calls = 0

    async def handler(task_id, payload):
        nonlocal calls
        calls += 1
        if calls <= resumes:
            assert tasks_runtime.park_awaiting_batch(task_id, ["batch_slow"])
            raise tasks_runtime.AwaitingBatch()
        raise OSError("No space left on device")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", handler)
    task_id = tasks_runtime.enqueue("test_kind", {})
    for _ in range(resumes):
        assert await worker.run_once() is True
        tasks_runtime.resume_parked(task_id)

    # task_max_attempts claims hit the error: every one but the last is requeued.
    for _ in range(max_attempts - 1):
        await worker.run_once()
        row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
        assert row["status"] == "pending"
    await worker.run_once()
    row = db.query_one("SELECT status FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "failed", "the budget is still finite"
    assert tasks_runtime.pending_batch_ids(task_id) == ["batch_slow"]


def test_transient_requeue_keeps_batch_ids_for_reattach():
    """The retry path is the reattach path: dropping the ids here would pay for
    the same batches twice."""
    task_id = tasks_runtime.enqueue("run_filter_batch_chunk", {"parent_id": 1})
    worker._claim_task()
    _hold_claim(task_id)
    hook = tasks_runtime.batch_event_hook(task_id, "filter", "gpt-5-nano")
    hook("batch_live", "in_progress", {"requests": 1, "completed": 0, "failed": 0})

    db.execute(
        "UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL "
        "WHERE id = %s AND status = 'running'",
        (task_id,),
    )
    assert tasks_runtime.pending_batch_ids(task_id) == ["batch_live"]


def test_progress_heartbeat_refuses_once_the_task_has_been_reclaimed():
    """set_progress carries the heartbeat, so a lost worker would otherwise
    keep vouching for the liveness of the run that replaced it."""
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    _hold_claim(task_id)
    _reclaim_elsewhere(task_id)
    db.execute("UPDATE tasks SET last_heartbeat = NULL WHERE id = %s", (task_id,))

    tasks_runtime.set_progress(task_id, 5, 10, "half way")
    row = db.query_one("SELECT last_heartbeat, progress FROM tasks WHERE id = %s", (task_id,))
    assert row["last_heartbeat"] is None
    assert row["progress"] is None


def test_progress_writes_while_the_claim_is_held():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    _hold_claim(task_id)
    tasks_runtime.set_progress(task_id, 5, 10, "half way")
    row = db.query_one("SELECT last_heartbeat, progress FROM tasks WHERE id = %s", (task_id,))
    assert row["last_heartbeat"] is not None
    assert row["progress"]["done"] == 5 and row["progress"]["label"] == "half way"


def test_park_waiting_is_refused_once_the_task_has_been_reclaimed():
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    _hold_claim(task_id)
    _reclaim_elsewhere(task_id)

    tasks_runtime.park_waiting(task_id, 10, "2 chunks across the fleet")
    row = db.query_one("SELECT status, progress FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "running", "must not park the run another worker holds"
    assert row["progress"] is None

    _hold_claim(task_id, attempts=2)
    db.execute("UPDATE tasks SET worker = %s WHERE id = %s", (worker.WORKER_NAME, task_id))
    tasks_runtime.park_waiting(task_id, 10, "2 chunks across the fleet")
    row = db.query_one("SELECT status, progress FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "waiting" and row["progress"]["total"] == 10


def test_lifecycle_writes_are_unrestricted_without_a_claim():
    """Nothing outside the worker loop claims tasks, so a direct call keeps the
    behaviour it had before ownership was enforced."""
    task_id = tasks_runtime.enqueue("run_filter", {})
    worker._claim_task()
    tasks_runtime.set_progress(task_id, 1, 2, "direct")
    tasks_runtime.finish(task_id, "done")
    row = db.query_one("SELECT status, progress FROM tasks WHERE id = %s", (task_id,))
    assert row["status"] == "done" and row["progress"]["done"] == 1
