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


def test_requirements_extraction_runs_only_when_switched_on(f):
    """Its one consumer is the market table and the deployed arm measured
    poorly, so the sweep is off until someone turns it on in config."""
    from api import worker

    worker.schedule_ingest_cycle()
    kinds = {r["kind"] for r in db.query("SELECT DISTINCT kind FROM tasks")}
    assert "extract_comp" in kinds and "extract_requirements" not in kinds
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('requirements_extraction_enabled', 'true') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    worker.schedule_ingest_cycle()
    assert db.query_one("SELECT 1 FROM tasks WHERE kind = 'extract_requirements'")


def test_mail_classification_is_not_scheduled_when_switched_off(f):
    from api import worker

    db.execute("UPDATE app_config SET value = 'false' WHERE key = 'mail_classification_enabled'")
    worker.schedule_ingest_cycle()
    assert not db.query_one("SELECT 1 FROM tasks WHERE kind = 'classify_mail'")


def test_a_board_is_recomputed_only_for_someone_who_can_have_one(f):
    """The cycle queued a recompute for every users row. One row that had
    signed in once, with no subscription, no board row and no upload, drew
    825 full recomputes in a day for zero rows, 60 percent of the real
    person's. A person is worth a recompute when the predicate can admit
    anything for them: a subscription, an acted-on row, or an upload."""
    from api import worker

    subscribed = f.make_user(sub="subscribed")
    uploader = f.make_user(sub="uploader")
    bare = f.make_user(sub="bare")
    f.make_source("s1")
    db.execute("INSERT INTO user_sources (user_id, source) VALUES (%s, %s)", (subscribed, "s1"))
    f.make_job(uploaded_by=uploader)
    worker.schedule_ingest_cycle()
    queued = {
        int(r["uid"])
        for r in db.query(
            "SELECT payload->>'user_id' AS uid FROM tasks WHERE kind = 'recompute_board'"
        )
    }
    assert subscribed in queued and uploader in queued and bare not in queued
