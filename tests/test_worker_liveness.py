"""A worker proves it is alive independently of what it is running.

The heartbeat was an asyncio task, so a handler declared `async def` that never
awaits blocked the event loop and the beat was never scheduled - the liveness
signal failed exactly when the work was longest. Measured live: mail_match has
no await anywhere, ran for 428 seconds, and the admin fleet view reported the
worker dead for all of it.
"""

from __future__ import annotations

import time

import pytest

from api import db, worker


def _worker_row():
    return db.query_one("SELECT * FROM worker_status WHERE name = %s", (worker.WORKER_NAME,))


def _beats_are_a_thread() -> bool:
    import inspect

    source = inspect.getsource(worker.run_once)
    return "threading.Thread" in source and "asyncio.create_task(_liveness" not in source


class TestTheBeatDoesNotDependOnTheHandler:
    def test_liveness_runs_on_a_thread_not_the_event_loop(self):
        """The fix, as a property rather than a timing test: a coroutine
        heartbeat cannot run while a handler blocks the loop, and two handler
        modules contain no await at all."""
        assert _beats_are_a_thread()

    @pytest.mark.asyncio
    async def test_a_handler_that_never_awaits_still_beats(self, monkeypatch, f):
        """The exact shape of mail_match: async def, zero awaits. Under the old
        coroutine heartbeat this test hangs the beat entirely."""
        tid = f.make_task("liveness_probe", {})
        beats: list[int] = []
        real_report = worker._report_worker_status

        def counting_report(task_id):
            beats.append(task_id or 0)
            real_report(task_id)

        monkeypatch.setattr(worker, "_report_worker_status", counting_report)
        monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.05)

        async def blocks_the_loop(task_id, payload):
            # No await. Exactly what an async handler with no await does to an
            # asyncio heartbeat.
            time.sleep(0.4)

        monkeypatch.setitem(worker.HANDLERS, "liveness_probe", blocks_the_loop)
        assert await worker.run_once() is True
        # The claim report fires once at claim time; the beats after it are the
        # ones a coroutine could never have produced.
        assert len(beats) >= 2, f"only {len(beats)} report(s): the beat did not run"
        assert tid

    @pytest.mark.asyncio
    async def test_the_beat_stops_when_the_task_ends(self, monkeypatch, f):
        """Joined rather than left running, so a beat cannot outlive the claim
        it vouches for and stamp a task the next iteration has moved on to."""
        f.make_task("liveness_probe", {})
        monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.05)

        async def quick(task_id, payload):
            return None

        monkeypatch.setitem(worker.HANDLERS, "liveness_probe", quick)
        await worker.run_once()
        alive = [t for t in __import__("threading").enumerate() if t.name == "liveness"]
        assert alive == [], "the liveness thread outlived its task"

    @pytest.mark.asyncio
    async def test_a_lost_claim_stops_the_beat(self, monkeypatch, f):
        """Beating on a reaped task would vouch for the run that replaced ours."""
        f.make_task("liveness_probe", {})
        monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.05)
        seen: list[int] = []

        async def steals_its_own_claim(task_id, payload):
            db.execute("UPDATE tasks SET attempts = attempts + 1 WHERE id = %s", (task_id,))
            time.sleep(0.3)
            seen.append(task_id)

        monkeypatch.setitem(worker.HANDLERS, "liveness_probe", steals_its_own_claim)
        await worker.run_once()
        assert seen, "the handler should still finish; only the beat stops"


class TestWorkerStatusIsTruthful:
    @pytest.mark.asyncio
    async def test_a_busy_worker_reports_its_task(self, monkeypatch, f):
        tid = f.make_task("liveness_probe", {})
        captured: list[int | None] = []

        async def records(task_id, payload):
            row = _worker_row()
            captured.append(row["current_task_id"] if row else None)

        monkeypatch.setitem(worker.HANDLERS, "liveness_probe", records)
        await worker.run_once()
        assert captured == [tid]

    @pytest.mark.asyncio
    async def test_an_idle_worker_still_reports(self, monkeypatch):
        """Idle is not dead, and the loop already got this right - the beat is
        what was missing, not the idle report."""
        db.execute("DELETE FROM tasks")
        assert await worker.run_once() is False
        row = _worker_row()
        assert row is not None and row["current_task_id"] is None
