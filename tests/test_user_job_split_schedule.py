"""The scheduler admits the legacy user_jobs split until one run completes."""

from __future__ import annotations

from api import db, telemetry, worker

KIND = "backfill_user_job_split"


def _split_tasks() -> list[dict]:
    return db.query(
        "SELECT status, payload->>'generation' AS generation FROM tasks WHERE kind = %s ORDER BY id",
        (KIND,),
    )


def _eligible_worker(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "RELEASE", "rel-split")
    db.execute(
        "INSERT INTO worker_status (name, started_at, last_seen, release) "
        "VALUES ('test-split', now(), now(), 'rel-split')"
    )


def test_scheduler_admits_once_continues_after_failure_and_stops_when_done(monkeypatch):
    _eligible_worker(monkeypatch)
    worker.schedule_ingest_cycle()
    worker.schedule_ingest_cycle()
    assert _split_tasks() == [{"status": "pending", "generation": "1"}], "one live run, not two"

    db.execute("UPDATE tasks SET status = 'failed' WHERE kind = %s", (KIND,))
    worker.schedule_ingest_cycle()
    assert _split_tasks() == [
        {"status": "failed", "generation": "1"},
        {"status": "pending", "generation": "2"},
    ]

    db.execute("UPDATE tasks SET status = 'done' WHERE kind = %s AND status = 'pending'", (KIND,))
    db.execute(
        "INSERT INTO tasks (kind, payload, status) VALUES (%s, '{\"generation\": 3}', 'cancelled')",
        (KIND,),
    )
    worker.schedule_ingest_cycle()
    assert len(_split_tasks()) == 3, "a completed run ends scheduling, even if a later one stopped"


def test_scheduler_skips_without_an_eligible_worker(monkeypatch):
    monkeypatch.setattr(telemetry, "RELEASE", "rel-nobody")
    worker.schedule_ingest_cycle()
    assert _split_tasks() == []
