"""progress_at: the only column that can say the WORK advanced.

A heartbeat is a thread beating on a timer. It proves the process is alive and
says nothing about whether the handler is moving, which is why a match_mail
task could sit 60 minutes at {"done": 0, "total": 1} with a 20-second-old
heartbeat and never be reaped.

This is instrumentation, not a control. Nothing reaps or alerts on it yet,
deliberately: no bound anyone has proposed survives contact with the data.
A duration bound is contaminated by the pathology it detects - three wedged
match_mail runs completed on 2026-09-03 and raised that kind's all-time
maximum from 5.2 minutes to 179.7, so a max-based threshold gets blinder every
time a wedge survives. And duration cannot work across kinds at all, because
ingest_source's runtime tracks payload size: two of them ran concurrently at
158 and 4.6 items/min under one kind name.

Progress staleness is payload-independent and cannot be contaminated. What it
needs is a bound, and the bound has to come from measuring how long real
handlers legitimately go between updates - which needs this column to exist
first. That is what this ships.
"""

from __future__ import annotations

from api import db
from api.tasks.runtime import _set_progress


def _task(kind: str = "match_mail") -> int:
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status, started_at, worker, attempts) "
        "VALUES (%s, '{}'::jsonb, 'running', now(), 'oci', 1) RETURNING id",
        (kind,),
    )
    assert row is not None
    return row["id"]


def _row(task_id: int) -> dict:
    row = db.query_one(
        "SELECT progress, progress_at, last_heartbeat FROM tasks WHERE id = %s", (task_id,)
    )
    assert row is not None
    return row


class TestProgressAtTracksMovementNotWrites:
    def test_the_first_report_stamps_it(self, f):
        task_id = _task()
        assert _row(task_id)["progress_at"] is None
        _set_progress(task_id, 0, 10, "starting")
        assert _row(task_id)["progress_at"] is not None

    def test_advancing_moves_it(self, f):
        task_id = _task()
        _set_progress(task_id, 1, 10, "working")
        first = _row(task_id)["progress_at"]
        _set_progress(task_id, 2, 10, "working")
        assert _row(task_id)["progress_at"] > first

    def test_reporting_the_same_numbers_again_does_NOT_move_it(self, f):
        """The whole point. A handler re-reporting where it already was has not
        advanced, and stamping it would make a stalled handler look identical
        to a working one - the timer-heartbeat mistake, one column along."""
        task_id = _task()
        _set_progress(task_id, 3, 10, "working")
        first = _row(task_id)["progress_at"]
        _set_progress(task_id, 3, 10, "working")
        assert _row(task_id)["progress_at"] == first

    def test_the_heartbeat_still_moves_when_progress_does_not(self, f):
        """Both facts stay available and they must be able to disagree - a
        wedged handler is exactly the case where the heartbeat is fresh and
        progress is stale."""
        task_id = _task()
        _set_progress(task_id, 3, 10, "working")
        before = _row(task_id)
        _set_progress(task_id, 3, 10, "working")
        after = _row(task_id)
        assert after["last_heartbeat"] > before["last_heartbeat"]
        assert after["progress_at"] == before["progress_at"]

    def test_a_changed_label_alone_counts_as_movement(self, f):
        """The label is part of what the handler reported. A handler that says
        something new about where it is has done something."""
        task_id = _task()
        _set_progress(task_id, 3, 10, "scraping")
        first = _row(task_id)["progress_at"]
        _set_progress(task_id, 3, 10, "parsing")
        assert _row(task_id)["progress_at"] > first

    def test_it_is_not_stamped_for_a_worker_that_lost_the_claim(self, f):
        """_set_progress already refuses to write for a lost claim so a stale
        worker cannot vouch for the run that replaced it. progress_at inherits
        that or it would carry the same lie.

        The guard only engages inside the worker loop, where a claim is in
        scope - a direct call has no claim to check and stays unrestricted, so
        the claim has to be set here or this tests nothing.
        """
        from api.tasks.runtime import TaskClaim, _current_claim

        task_id = _task()
        token = _current_claim.set(TaskClaim(task_id=task_id, worker="oci", attempts=1))
        try:
            _set_progress(task_id, 1, 10, "working")
            first = _row(task_id)["progress_at"]
            assert first is not None
            db.execute("UPDATE tasks SET attempts = 2 WHERE id = %s", (task_id,))
            _set_progress(task_id, 9, 10, "working")
            assert _row(task_id)["progress_at"] == first
        finally:
            _current_claim.reset(token)
