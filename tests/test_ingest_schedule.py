"""schedule_ingest_cycle: one queued ingest per source, however far behind."""

from __future__ import annotations

from api import db


def _pending(source: str) -> int:
    row = db.query_one(
        "SELECT count(*) AS n FROM tasks WHERE kind = 'ingest_source' AND status = 'pending' "
        "AND payload->>'source' = %s",
        (source,),
    )
    assert row is not None
    return row["n"]


def test_a_source_still_queued_from_the_last_cycle_is_not_queued_again(monkeypatch, f):
    """The dedupe key is per cycle, so a queue that falls behind the hour used
    to grow by one task per source per hour. A pending ingest is the whole
    of what the next cycle would add; a running one is not, because its
    successor is claimed the moment it finishes."""
    from api import worker

    f.make_source("queued")
    f.make_source("in_flight")
    f.make_source("idle")
    f.make_source("off", active=False)
    f.make_task("ingest_source", {"source": "queued", "cycle": "old"}, status="pending")
    f.make_task("ingest_source", {"source": "in_flight", "cycle": "old"}, status="running")

    worker.schedule_ingest_cycle()

    assert _pending("queued") == 1
    assert _pending("in_flight") == 1
    assert _pending("idle") == 1
    assert _pending("off") == 0

    # The same cycle again adds nothing: the per-cycle dedupe still holds.
    worker.schedule_ingest_cycle()
    assert _pending("idle") == 1


def test_a_daily_source_is_pulled_once_a_day_and_a_failure_does_not_count(f):
    """A source on a longer interval than the cycle waits while its last
    successful or in-flight pull is younger than the interval. A failed pull
    is not a pull, so the next cycle retries it rather than tomorrow."""
    from api import worker

    for name in ("done_recently", "done_long_ago", "failed_recently", "running_now"):
        f.make_source(name)
    db.execute("UPDATE sources SET ingest_interval_hours = 24")
    db.execute(
        """
        INSERT INTO tasks (kind, payload, status, created_at) VALUES
          ('ingest_source', %s, 'done', now() - interval '2 hours'),
          ('ingest_source', %s, 'done', now() - interval '30 hours'),
          ('ingest_source', %s, 'failed', now() - interval '2 hours'),
          ('ingest_source', %s, 'running', now() - interval '10 minutes')
        """,
        (
            db.jsonb({"source": "done_recently"}),
            db.jsonb({"source": "done_long_ago"}),
            db.jsonb({"source": "failed_recently"}),
            db.jsonb({"source": "running_now"}),
        ),
    )

    worker.schedule_ingest_cycle()

    assert _pending("done_recently") == 0
    assert _pending("running_now") == 0
    assert _pending("done_long_ago") == 1
    assert _pending("failed_recently") == 1
