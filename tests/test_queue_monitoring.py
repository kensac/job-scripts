"""The queue as something a person can see: two detectors for it not moving,
the admin summaries of it and of what ingest delivered, and the per-kind
gauges. Each detector has the case that must fire and the one that must not."""

from __future__ import annotations

from api import db, health, metrics


def _worker(name: str, *, idle: bool, seen_minutes_ago: float = 0.5) -> None:
    db.execute(
        "INSERT INTO worker_status (name, current_task_id, last_seen) "
        "VALUES (%s, %s, now() - make_interval(secs => %s)) "
        "ON CONFLICT (name) DO UPDATE SET current_task_id = EXCLUDED.current_task_id, "
        "last_seen = EXCLUDED.last_seen",
        (name, None if idle else 1, seen_minutes_ago * 60),
    )


def _pending(kind: str, minutes_ago: float, payload: dict | None = None) -> None:
    db.execute(
        "INSERT INTO tasks (kind, payload, status, created_at) "
        "VALUES (%s, %s, 'pending', now() - make_interval(mins => %s))",
        (kind, db.jsonb(payload or {}), minutes_ago),
    )


def _queue_alerts():
    return {(a["kind"], a["subject"]) for a in health._detect_queue()}


def test_an_idle_worker_beside_old_pending_work_is_a_stall_and_a_busy_one_is_not():
    _worker("idle-fresh", idle=True)
    _worker("busy", idle=False)
    # Gone, not idle: the reaper's problem, not this detector's.
    _worker("idle-stale", idle=True, seen_minutes_ago=30)
    assert _queue_alerts() == set()
    _pending("classify_mail", 3)
    # Three minutes is one poll's worth of latency, not a stall.
    assert _queue_alerts() == set()
    _pending("classify_mail", 15)
    assert _queue_alerts() == {("queue_stalled", "idle-fresh")}
    # The threshold is persisted config, not a constant.
    db.execute("UPDATE app_config SET value = '30' WHERE key = 'queue_stall_minutes'")
    assert _queue_alerts() == set()


def test_ingest_pending_past_two_cycles_is_a_backlog_and_within_one_is_not(monkeypatch):
    from api.tasks import runtime

    monkeypatch.setattr(runtime, "INGEST_INTERVAL_MINUTES", 60)
    _pending("ingest_source", 90, {"source": "a"})
    assert _queue_alerts() == set()
    _pending("ingest_source", 150, {"source": "b"})
    alerts = health._detect_queue()
    assert [(a["kind"], a["subject"]) for a in alerts] == [("ingest_backlog", "ingest_source")]
    assert alerts[0]["detail"]["pending"] == 2 and alerts[0]["detail"]["limit_minutes"] == 120
    db.execute("UPDATE app_config SET value = '3' WHERE key = 'ingest_backlog_cycles'")
    assert _queue_alerts() == set()


def test_the_queue_summary_says_what_waits_who_works_and_how_fast(client, admin_headers, f):
    f.make_source("acme")
    _pending("ingest_source", 40, {"source": "acme"})
    _pending("ingest_source", 5, {"source": "acme"})
    _pending("verify_new", 1)
    running = f.make_task("ingest_source", {"source": "acme"}, status="running")
    db.execute("UPDATE tasks SET worker = 'hetzner', started_at = now() WHERE id = %s", (running,))
    for status, secs in (("done", 30), ("done", 90), ("failed", 10)):
        tid = f.make_task("ingest_source", {"source": "acme"}, status=status)
        db.execute(
            "UPDATE tasks SET worker = 'oci', started_at = now() - make_interval(secs => %s), "
            "finished_at = now() WHERE id = %s",
            (secs, tid),
        )
    _worker("hetzner", idle=False)
    _worker("oci", idle=True)

    body = client.get("/v1/admin/queue", params={"hours": 2}, headers=admin_headers).json()
    assert body["hours"] == 2
    assert {(r["kind"], r["pending"]) for r in body["pending"]} == {
        ("ingest_source", 2),
        ("verify_new", 1),
    }
    ingest = next(r for r in body["pending"] if r["kind"] == "ingest_source")
    assert ingest["oldest_minutes"] == 40
    assert [(r["kind"], r["worker"], r["source"]) for r in body["in_flight"]] == [
        ("ingest_source", "hetzner", "acme")
    ]
    (t,) = body["throughput"]
    assert (t["worker"], t["kind"], t["done"], t["failed"]) == ("oci", "ingest_source", 2, 1)
    assert 40 <= t["avg_seconds"] <= 46
    assert {(w["name"], w["fresh"]) for w in body["workers"]} == {("hetzner", True), ("oci", True)}
    # The window is bounded, not trusted.
    assert (
        client.get("/v1/admin/queue", params={"hours": 99999}, headers=admin_headers).json()[
            "hours"
        ]
        == 24 * 7
    )


def test_the_ingest_summary_reads_the_counts_each_pull_left_behind(client, admin_headers, f):
    f.make_source("board")
    f.make_source("mirror")
    db.execute("INSERT INTO source_groups (name, members) VALUES ('b', ARRAY['board'])")
    for name, progress in (
        ("board", {"fetched": 400, "kept": 40, "cached": 12, "fetch_failed": 3, "gone": 1}),
        ("board", {"fetched": 410, "kept": 42, "cached": 2, "fetch_failed": 0, "gone": 0}),
        ("mirror", {"fetched": 2900, "kept": 2900, "cached": 0, "fetch_failed": 0, "gone": 0}),
    ):
        tid = f.make_task("ingest_source", {"source": name}, status="done")
        db.execute(
            "UPDATE tasks SET finished_at = now(), progress = %s WHERE id = %s",
            (db.jsonb({"done": 0, "total": 0, "label": name, **progress}), tid),
        )
    # A failed pull counts as a pull and leaves no counts.
    tid = f.make_task("ingest_source", {"source": "board"}, status="failed")
    db.execute("UPDATE tasks SET finished_at = now() WHERE id = %s", (tid,))
    f.make_job(source="board", url="https://board.test/1")
    f.make_job(source="board", url="https://board.test/2")

    body = client.get("/v1/admin/ingest", params={"hours": 24}, headers=admin_headers).json()
    rows = {r["name"]: r for r in body["rows"]}
    assert rows["board"]["groups"] == ["b"]
    assert (rows["board"]["pulls"], rows["board"]["failed_pulls"]) == (3, 1)
    assert (rows["board"]["fetched"], rows["board"]["kept"], rows["board"]["cached"]) == (
        810,
        82,
        14,
    )
    assert (rows["board"]["fetch_failed"], rows["board"]["gone"], rows["board"]["new_jobs"]) == (
        3,
        1,
        2,
    )
    # Mirror: pulled and fetched plenty, produced nothing new. Visible as such.
    assert (rows["mirror"]["fetched"], rows["mirror"]["new_jobs"]) == (2900, 0)
    assert body["totals"]["fetched"] == 3710 and body["totals"]["new_jobs"] == 2
    # Ordered by what was actually new.
    assert body["rows"][0]["name"] == "board"


def test_queue_gauges_read_zero_for_a_kind_that_drained():
    _pending("classify_mail", 10)
    metrics.refresh_queue_gauges()
    assert metrics.TASK_QUEUE_BY_KIND.labels("classify_mail", "pending")._value.get() == 1
    assert metrics.OLDEST_PENDING_AGE_BY_KIND.labels("classify_mail")._value.get() >= 600
    db.execute("UPDATE tasks SET status = 'done' WHERE kind = 'classify_mail'")
    metrics.refresh_queue_gauges()
    assert metrics.TASK_QUEUE_BY_KIND.labels("classify_mail", "pending")._value.get() == 0
    assert metrics.OLDEST_PENDING_AGE_BY_KIND.labels("classify_mail")._value.get() == 0


def test_the_two_thresholds_are_admin_config(client, admin_headers):
    cfg = client.get("/v1/admin/config", headers=admin_headers).json()["config"]
    assert (cfg["queue_stall_minutes"], cfg["ingest_backlog_cycles"]) == (10, 2)
    r = client.put(
        "/v1/admin/config/ingest_backlog_cycles", json={"value": 3}, headers=admin_headers
    )
    assert r.status_code == 200 and db.get_config("ingest_backlog_cycles") == 3
    r = client.put("/v1/admin/config/queue_stall_minutes", json={"value": 0}, headers=admin_headers)
    assert r.status_code == 400
