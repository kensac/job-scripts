"""schedule_ingest_cycle: one queued ingest per source, however far behind."""

from __future__ import annotations

import datetime

from api import db, queue


def _pending(source: str) -> int:
    row = db.query_one(
        "SELECT count(*) AS n FROM tasks WHERE kind = 'ingest_source' AND status = 'pending' "
        "AND payload->>'source' = %s",
        (source,),
    )
    assert row is not None
    return row["n"]


def _anchor_hours_ago(hours: int) -> None:
    """Put daily_ingest_hour_eastern `hours` (to `hours` + 1) before now, so a
    test does not depend on the time of day the suite runs."""
    now = datetime.datetime.now(queue.DAILY_PULL_ZONE)
    hour = (now - datetime.timedelta(hours=hours)).hour
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('daily_ingest_hour_eastern', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb(hour),),
    )


def _pulled(source: str, status: str, hours_ago: float) -> None:
    db.execute(
        "INSERT INTO tasks (kind, payload, status, created_at) "
        "VALUES ('ingest_source', %s, %s, now() - make_interval(secs => %s))",
        (db.jsonb({"source": source}), status, hours_ago * 3600),
    )


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


def test_a_daily_source_is_pulled_once_a_day_and_a_failure_counts_too(f):
    """A source on a longer interval than the cycle waits while its last
    successful or in-flight pull is younger than the interval. A failed pull
    counts as well: retried every cycle, a board answering 404 was pulled 24
    times a day (tests/test_ingest_backoff.py has the run that backs off)."""
    from api import worker

    for name in ("done_recently", "done_long_ago", "failed_recently", "running_now"):
        f.make_source(name)
    db.execute("UPDATE sources SET ingest_interval_hours = 24")
    _anchor_hours_ago(5)
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
    assert _pending("failed_recently") == 0


def test_a_daily_source_is_pulled_at_the_configured_hour_not_a_day_after_its_last_pull(f):
    """An age check alone kept every daily board at the hour its first pull
    landed: 7,740 of their pulls in two days were queued in the 8 pm Eastern
    hour, so batched verdicts arrived the next morning. A board pulled before
    today's hour is due at that hour even though less than a day has passed;
    one pulled since is not. Whole-day intervals count back from the hour;
    other intervals keep the age check."""
    from api import worker

    _anchor_hours_ago(5)
    for name, interval, status, hours_ago in (
        ("daily_before_hour", 24, "done", 7),
        ("daily_since_hour", 24, "done", 3),
        ("weekly_recent", 168, "done", 72),
        ("weekly_stale", 168, "done", 8 * 24),
        ("four_hourly_recent", 4, "done", 3),
        ("four_hourly_stale", 4, "done", 5),
    ):
        f.make_source(name)
        db.execute(
            "UPDATE sources SET ingest_interval_hours = %s WHERE name = %s", (interval, name)
        )
        _pulled(name, status, hours_ago)

    worker.schedule_ingest_cycle()

    assert _pending("daily_before_hour") == 1
    assert _pending("daily_since_hour") == 0
    assert _pending("weekly_recent") == 0
    assert _pending("weekly_stale") == 1
    assert _pending("four_hourly_recent") == 0
    assert _pending("four_hourly_stale") == 1


def test_the_daily_anchor_keeps_its_wall_clock_hour_across_dst(f):
    """5 pm Eastern is 21:00 UTC in October and 22:00 UTC in November. The
    anchor before the hour is yesterday's, at yesterday's offset."""
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('daily_ingest_hour_eastern', '17') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    utc = datetime.UTC

    def anchor(*when: int) -> datetime.datetime:
        return queue.daily_pull_anchor(datetime.datetime(*when, tzinfo=utc)).astimezone(utc)

    # DST ends 2026-11-01 02:00 Eastern.
    assert anchor(2026, 10, 14, 21, 30) == datetime.datetime(2026, 10, 14, 21, tzinfo=utc)
    assert anchor(2026, 10, 14, 20, 59) == datetime.datetime(2026, 10, 13, 21, tzinfo=utc)
    assert anchor(2026, 11, 1, 15, 0) == datetime.datetime(2026, 10, 31, 21, tzinfo=utc)
    assert anchor(2026, 11, 2, 12, 0) == datetime.datetime(2026, 11, 1, 22, tzinfo=utc)


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


def test_mail_matching_is_scheduled_only_after_a_change(f):
    from api import worker

    def queued() -> int:
        row = db.query_one(
            "SELECT count(*) AS n FROM tasks WHERE kind = 'match_mail' AND status = 'pending'"
        )
        assert row is not None
        return row["n"]

    # Never swept: the first sweep runs.
    worker.schedule_ingest_cycle()
    assert queued() == 1
    # The dedupe key is the cycle's; cleared so only the change check decides.
    db.execute(
        "UPDATE tasks SET status = 'done', started_at = now(), dedupe_key = NULL "
        "WHERE kind = 'match_mail'"
    )
    worker.schedule_ingest_cycle()
    assert queued() == 0, "nothing changed since the last sweep"

    uid = f.make_user()
    db.execute(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, 'Acme', 'tracker')",
        (uid,),
    )
    worker.schedule_ingest_cycle()
    assert queued() == 1


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


def test_a_person_with_a_recompute_still_queued_gets_no_second_one(f):
    """The dedupe key is per board_refresh_minutes bucket, so a queue that
    fell behind grew by one recompute per person per bucket: 66 were pending
    for three people on 2026-10-10. A running one does not block, because it
    may have read the board before what changed."""
    from api import worker
    from api.board import visibility

    f.make_source("s1")
    queued = f.make_user(sub="queued")
    running = f.make_user(sub="running")
    for uid in (queued, running):
        db.execute("INSERT INTO user_sources (user_id, source) VALUES (%s, %s)", (uid, "s1"))
    f.make_task("recompute_board", {"user_id": queued, "cycle": "old"}, status="pending")
    f.make_task("recompute_board", {"user_id": running, "cycle": "old"}, status="running")

    worker.schedule_ingest_cycle()
    visibility.request_refresh(queued)

    pending = db.query(
        "SELECT payload->>'user_id' AS uid FROM tasks "
        "WHERE kind = 'recompute_board' AND status = 'pending'"
    )
    assert sorted(int(r["uid"]) for r in pending) == sorted([queued, running])
