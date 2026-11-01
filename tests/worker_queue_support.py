"""Shared worker claim and provider batch fixtures for worker behavior tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import db, worker
from tasks import runtime as tasks_runtime


@pytest.fixture(autouse=True)
def _clear_claim():
    """The claim is a contextvar the worker loop owns; a test that sets one by
    hand must not leak it into the next test."""
    yield
    tasks_runtime.set_current_claim(None)


def _hold_claim(task_id: int, attempts: int = 1) -> None:
    tasks_runtime.set_current_claim(tasks_runtime.TaskClaim(task_id, worker.WORKER_NAME, attempts))


def _reclaim_elsewhere(task_id: int) -> None:
    """What the reaper plus another host do to a task whose worker went stale:
    back to 'pending', then claimed again - which is the state the lost worker
    cannot distinguish from its own."""
    db.execute(
        "UPDATE tasks SET status = 'pending', started_at = NULL, last_heartbeat = NULL "
        "WHERE id = %s",
        (task_id,),
    )
    db.execute(
        "UPDATE tasks SET status = 'running', worker = 'other-host', "
        "attempts = attempts + 1, started_at = now(), last_heartbeat = now() "
        "WHERE id = %s",
        (task_id,),
    )


# What the stand-in provider last accepted, read back by `_collect_from`.
_submitted = SimpleNamespace(specs=[], calls=0)


async def _submit_ids(specs, model, effort, max_out, on_event=None):
    """Stands in for the provider accepting a submission."""
    _submitted.specs = list(specs)
    _submitted.calls += 1
    batch_id = f"batch_test_{_submitted.calls}"
    if on_event:
        on_event(batch_id, "submitted", {"requests": len(specs)})
    return [batch_id]


def _collect_from(fake_batch):
    """Turns a test's existing result-builder into a collector, so the
    per-test expectations stay exactly as they were written."""

    async def _collect(batch_ids, on_event=None):
        results = await fake_batch(_submitted.specs, "m", "low", 0, None)
        for result in results.values():
            result.batch_id = batch_ids[0]
        return list(results.values())

    return _collect


def _claim_for_test(task_id: int) -> int:
    """What _claim_task does to one specific row, plus the claim the loop holds
    for the duration of the handler."""
    row = db.query_one(
        "UPDATE tasks SET status = 'running', started_at = now(), last_heartbeat = now(), "
        "attempts = attempts + 1, worker = %s WHERE id = %s RETURNING attempts",
        (worker.WORKER_NAME, task_id),
    )
    assert row is not None
    _hold_claim(task_id, row["attempts"])
    return row["attempts"]


async def _run_batched(task_id, coro_factory):
    """Runs a batched handler through claim -> submit -> park -> resume -> collect.

    The first call submits and raises AwaitingBatch (freeing the worker in
    production); poll_batches then resumes the task, another claim picks it up
    and the second call finds the batch ids in the task payload and collects.
    Driving every hop is what makes these tests exercise the real lifecycle
    rather than a submit-and-wait that no longer exists - parking in particular
    is a write to a task the worker owns, so a handler that never claimed one
    is not a state the loop can reach.
    """
    from api.worker import AwaitingBatch

    _claim_for_test(task_id)
    try:
        await coro_factory()
    except AwaitingBatch:
        tasks_runtime.resume_parked(task_id)
        _claim_for_test(task_id)
        await coro_factory()
