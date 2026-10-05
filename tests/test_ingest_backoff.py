"""A board whose pulls keep failing is pulled less often, then switched off."""

from __future__ import annotations

import asyncio

import pytest
import requests

from api import db, health, queue, worker
from core.fetching import boards
from tasks import ingest


def _pull(source: str, status: str, age_hours: float, error: str | None = None, **counts) -> None:
    db.execute(
        """
        INSERT INTO tasks (kind, payload, status, error, progress, created_at, finished_at)
        VALUES ('ingest_source', %s, %s, %s, %s,
                now() - make_interval(secs => %s), now() - make_interval(secs => %s))
        """,
        (
            db.jsonb({"source": source}),
            status,
            error,
            db.jsonb({"done": 0, "total": 0, **counts}) if counts else None,
            age_hours * 3600,
            age_hours * 3600,
        ),
    )


def _interval(source: str, hours: int) -> None:
    db.execute("UPDATE sources SET ingest_interval_hours = %s WHERE name = %s", (hours, source))


def _scheduled() -> set[str]:
    worker.schedule_ingest_cycle()
    return {
        r["s"]
        for r in db.query(
            "SELECT payload->>'source' AS s FROM tasks "
            "WHERE kind = 'ingest_source' AND status = 'pending'"
        )
    }


def test_the_wait_doubles_from_the_interval_up_to_the_cap():
    assert [queue.pull_wait_hours(1, k) for k in range(1, 9)] == [1, 2, 4, 8, 16, 32, 64, 72]
    assert [queue.pull_wait_hours(24, k) for k in range(1, 5)] == [24, 48, 72, 72]
    # A weekly board is never pulled sooner because it failed.
    assert queue.pull_wait_hours(168, 1) == 168


def test_a_failed_pull_counts_toward_the_interval_and_a_run_backs_off(f):
    for name in ("failed_once", "failed_twice", "waited_enough", "healthy", "given_up"):
        f.make_source(name)
        _interval(name, 24)
    # One failure 30 hours ago: a daily board's wait is its interval, so due.
    _pull("waited_enough", "failed", 30)
    # One failure 3 hours ago: it counts toward the interval like a success.
    _pull("failed_once", "failed", 3)
    # Two in a row, the latest 30 hours ago: the wait is 48 hours.
    _pull("failed_twice", "failed", 60)
    _pull("failed_twice", "failed", 30)
    # A success ends the run: the failures before it count for nothing.
    _pull("healthy", "failed", 80)
    _pull("healthy", "failed", 50)
    _pull("healthy", "done", 30, fetched=3)
    # At the give-up count the next pull is due now: it either succeeds or
    # switches the board off.
    for h in range(8, 0, -1):
        _pull("given_up", "failed", h)

    assert _scheduled() == {"waited_enough", "healthy", "given_up"}


def test_a_pull_that_never_asked_the_board_is_not_a_failure(f):
    f.make_source("was_off")
    for h in range(1, 10):
        _pull("was_off", "failed", h / 10, error=queue.INACTIVE_SOURCE_ERROR)
    assert queue.failure_runs(["was_off"]) == {}
    assert "was_off" in _scheduled()


def _failing_fetch(monkeypatch):
    def fetch_listings(url, company=None):
        response = requests.Response()
        response.status_code = 404
        raise requests.HTTPError("404 Client Error: Not Found", response=response)

    monkeypatch.setattr(boards, "fetch_listings", fetch_listings)


def _ingest(f, source: str) -> None:
    task = f.make_task("ingest_source", {"source": source}, status="running")
    with pytest.raises(requests.HTTPError):
        asyncio.run(ingest.handle_ingest_source(task, {"source": source}))


def _active(source: str) -> bool:
    return db.query_one("SELECT active FROM sources WHERE name = %s", (source,))["active"]


def test_the_failure_that_reaches_the_give_up_count_switches_the_board_off(f, monkeypatch):
    _failing_fetch(monkeypatch)
    f.make_source("dead")
    f.make_source("flaky")
    for h in range(7, 0, -1):
        _pull("dead", "failed", h, error="404")
        _pull("flaky", "failed", h, error="404")
    _pull("flaky", "done", 0.5, fetched=4)

    _ingest(f, "dead")
    _ingest(f, "flaky")

    assert not _active("dead"), "the eighth failure in a row switches it off"
    assert _active("flaky"), "a success ended its run, so this is its first failure"


def test_a_switched_off_board_is_an_alert_until_it_is_switched_back_on(f):
    f.make_source("dead", active=False)
    f.make_source("failing")
    f.make_source("off_by_hand", active=False)
    for h in range(8, 0, -1):
        _pull("dead", "failed", h, error="404 Client Error")
    for h in (3, 2, 1):
        _pull("failing", "failed", h, error="404 Client Error")
        # Switched off by a person after three failures: no claim it gave up.
        _pull("off_by_hand", "failed", h, error="404 Client Error")

    kinds = {(a["kind"], a["subject"]) for a in health._detect_boards()}
    assert kinds == {("source_switched_off", "dead"), ("ingest_failing", "failing")}

    db.execute("UPDATE sources SET active = true WHERE name = 'dead'")
    kinds = {(a["kind"], a["subject"]) for a in health._detect_boards()}
    assert ("source_switched_off", "dead") not in kinds
    assert ("ingest_failing", "dead") in kinds


def test_a_board_that_never_listed_anything_is_surfaced_once_for_the_set(f):
    for name in ("empty_a", "empty_b", "has_jobs", "new_board", "once_listed"):
        f.make_source(name)
    db.execute(
        "UPDATE sources SET created_at = now() - interval '30 days' WHERE name <> 'new_board'"
    )
    for name in ("empty_a", "empty_b", "has_jobs", "new_board", "once_listed"):
        _pull(name, "done", 2, fetched=0, kept=0)
        _pull(name, "done", 50, fetched=0, kept=0)
    f.make_job(source="has_jobs")
    db.execute(
        "INSERT INTO listings (url, source, pattern, kept) "
        "VALUES ('https://jobs.test/x', 'once_listed', '', false)"
    )

    found = [a for a in health._detect_boards() if a["kind"] == "sources_never_produced"]
    assert len(found) == 1
    assert found[0]["detail"]["sources"] == ["empty_a", "empty_b"]
