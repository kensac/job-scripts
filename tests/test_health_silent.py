"""health._detect_silent and the detector wrapper: work that reports success
while doing nothing, from the audit of 2026-09-05. Every detector is made to
fire, and its must-not case sits beside it."""

from __future__ import annotations

from api import db, health, mail


def _task(kind, status, *, progress=None, attempts=0, age_hours=1, error=None):
    db.execute(
        """
        INSERT INTO tasks (kind, payload, status, attempts, error, progress, created_at,
                           finished_at)
        VALUES (%s, '{}', %s, %s, %s, %s, now() - make_interval(hours => %s),
                CASE WHEN %s IN ('done', 'failed') THEN now() - make_interval(hours => %s) END)
        """,
        (
            kind,
            status,
            attempts,
            error,
            db.jsonb(progress) if progress else None,
            age_hours,
            status,
            age_hours,
        ),
    )


def _silent():
    return {(a["kind"], a["subject"]) for a in health._detect_silent()}


def test_a_sweep_that_finished_with_nothing_written_fires():
    _task("extract_comp", "done", progress={"done": 0, "total": 300, "label": "comp extracted"})
    # Nothing to do is not nothing done; and partial progress is progress.
    _task("extract_requirements", "done", progress={"done": 0, "total": 0, "label": "nothing"})
    _task("verify_new", "done", progress={"done": 5, "total": 300, "label": "x"})
    # A poll that found no batch terminal did its job; so did a sync with
    # nothing new. Only a sweep whose done is a row written counts.
    _task("poll_batches", "done", progress={"done": 0, "total": 8, "label": "polled"})
    _task("sync_gmail", "done", progress={"done": 0, "total": 500, "label": "synced"})
    assert _silent() == {("sweep_did_nothing", "extract_comp")}


def test_managed_board_batch_is_an_honest_sweep():
    _task("run_managed_board_batch", "done", progress={"done": 0, "total": 20})
    assert _silent() == {("sweep_did_nothing", "run_managed_board_batch")}


def test_completed_sweep_with_invalid_progress_says_it_cannot_tell():
    _task("extract_comp", "done", progress={"done": "unknown", "total": 20})
    _task("verify_new", "done", progress={"done": 21, "total": 20})
    assert _silent() == {
        ("task_progress_invalid", "extract_comp"),
        ("task_progress_invalid", "verify_new"),
    }


def test_running_task_with_fresh_heartbeat_and_stale_progress_fires():
    db.execute(
        """
        INSERT INTO tasks (kind, payload, status, worker, started_at, last_heartbeat, progress_at,
                           progress)
        VALUES ('match_mail', '{}', 'running', 'worker', now() - interval '2 hours', now(),
                now() - interval '31 minutes', '{"done": 1, "total": 2}')
        """
    )
    assert _silent() == {("task_progress_stalled", "1")}


def test_running_task_without_first_progress_uses_its_start_time():
    db.execute(
        """
        INSERT INTO tasks (kind, payload, status, worker, started_at, last_heartbeat)
        VALUES ('match_mail', '{}', 'running', 'worker', now() - interval '31 minutes', now())
        """
    )
    assert _silent() == {("task_progress_stalled", "1")}


def test_recent_or_dead_worker_is_not_a_progress_stall():
    db.execute(
        """
        INSERT INTO tasks (kind, payload, status, worker, started_at, last_heartbeat, progress_at)
        VALUES ('match_mail', '{}', 'running', 'fresh', now() - interval '20 minutes', now(),
                now() - interval '20 minutes'),
               ('match_mail', '{}', 'running', 'dead', now() - interval '2 hours',
                now() - interval '10 minutes', now() - interval '2 hours')
        """
    )
    assert _silent() == set()


def test_a_kind_failing_three_times_fires_but_ingest_has_its_own():
    for _ in range(3):
        _task("probe_credentials", "failed", error="InvalidToken")
        _task("ingest_source", "failed", error="429")
    _task("extract_comp", "failed", error="once")
    assert _silent() == {("task_kind_failing", "probe_credentials")}


def test_budget_refusals_are_not_task_failures_an_operator_can_fix():
    for _ in range(3):
        _task("run_all_filters", "failed", error="BUDGET_EXCEEDED after 0/100 checks")
    assert _silent() == set()


def test_a_task_the_reaper_keeps_handing_back_fires():
    _task("classify_mail", "pending", attempts=4, age_hours=7)
    _task("classify_mail", "pending", attempts=4, age_hours=1)
    _task("classify_mail", "pending", attempts=1, age_hours=7)
    kinds = {k for k, _ in _silent()}
    assert kinds == {"task_requeued_forever"}
    assert len(_silent()) == 1


def test_an_alert_nobody_was_told_about_fires_only_when_mail_is_configured(monkeypatch):
    db.execute(
        "INSERT INTO health_alerts (kind, subject, severity, message, first_seen) "
        "VALUES ('ingest_failing', 'x', 'warning', 'm', now() - interval '2 hours')"
    )
    monkeypatch.setattr(mail, "configured", lambda: False)
    assert _silent() == set()
    monkeypatch.setattr(mail, "configured", lambda: True)
    assert _silent() == {("alerts_unnotified", "_notify")}


def test_a_pattern_that_admits_everything_fires(f):
    f.make_source("loose")
    f.make_source("small")
    db.execute("UPDATE sources SET title_pattern = '.*' WHERE name IN ('loose', 'small')")
    db.execute(
        """
        INSERT INTO tasks (kind, payload, status, progress, created_at, finished_at)
        VALUES ('ingest_source', %s, 'done', %s, now() - interval '1 hour', now() - interval '1 hour'),
               ('ingest_source', %s, 'done', %s, now() - interval '1 hour', now() - interval '1 hour')
        """,
        (
            db.jsonb({"source": "loose"}),
            db.jsonb({"done": 0, "total": 0, "fetched": 80, "kept": 80}),
            db.jsonb({"source": "small"}),
            db.jsonb({"done": 0, "total": 0, "fetched": 8, "kept": 8}),
        ),
    )
    found = {(a["kind"], a["subject"]) for a in health._detect_boards()}
    assert ("source_pattern_admits_all", "loose") in found
    assert ("source_pattern_admits_all", "small") not in found


def test_a_detector_that_raises_is_an_alert_not_silence(monkeypatch):
    def boom():
        raise RuntimeError("column vanished")

    monkeypatch.setattr(health, "_detect_queue", boom)
    found = health.detect()
    failed = [a for a in found if a["kind"] == "detector_failed"]
    assert len(failed) == 1 and failed[0]["subject"] == "_detect_queue"
    assert "column vanished" in failed[0]["message"]
