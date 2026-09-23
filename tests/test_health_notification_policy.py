from datetime import UTC, datetime, timedelta

import pytest

from api import db, health, mail
from tasks import health as health_task


def test_digest_catches_up_and_stall_ids_share_a_cooldown():
    from api.health.notifications import select_due

    now = datetime(2026, 9, 23, 15, tzinfo=UTC)
    alert = {
        "kind": "task_progress_stalled",
        "subject": "2",
        "severity": "warning",
        "first_seen": now - timedelta(hours=4),
        "detail": {"kind": "verify_new"},
    }
    assert len(select_due([alert], [], now, 13, 24)) == 1
    previous = {**alert, "subject": "1", "notified_at": now - timedelta(hours=3)}
    assert select_due([alert], [previous], now, 13, 24) == []
    assert len(select_due([alert, {**alert, "subject": "3"}], [], now, 13, 24)) == 1
    assert len(select_due([{**alert, "severity": "critical"}], [previous], now, 13, 24)) == 1
    assert (
        select_due(
            [{**alert, "severity": "critical"}], [{**previous, "severity": "critical"}], now, 13, 24
        )
        == []
    )


def test_newer_stalls_cannot_postpone_an_already_due_incident():
    from api.health.notifications import select_due

    now = datetime(2026, 9, 23, 15, tzinfo=UTC)
    older = {
        "id": 1,
        "kind": "task_progress_stalled",
        "subject": "1",
        "severity": "warning",
        "first_seen": now - timedelta(hours=4),
        "detail": {"kind": "verify_new"},
    }
    newer = {**older, "id": 2, "subject": "2", "first_seen": now - timedelta(minutes=5)}
    assert [r["id"] for r in select_due([newer, older], [], now, 13, 24)] == [1]


def test_finished_task_stall_resolves_without_waiting_for_grace(f):
    task_id = f.make_task("verify_new")
    db.execute("UPDATE tasks SET status='running' WHERE id=%s", (task_id,))
    finding = {
        "kind": "task_progress_stalled",
        "subject": str(task_id),
        "severity": "warning",
        "message": "Stalled",
        "detail": {"kind": "verify_new"},
    }
    health.record([finding])
    db.execute("UPDATE tasks SET status='failed' WHERE id=%s", (task_id,))
    health.record([])
    assert (
        db.query_one("SELECT resolved_at FROM health_alerts WHERE subject=%s", (str(task_id),))[
            "resolved_at"
        ]
        is not None
    )


@pytest.mark.asyncio
async def test_warning_waits_for_digest_without_being_marked_delivered(f, monkeypatch):
    row = db.query_one("SELECT EXTRACT(HOUR FROM now() AT TIME ZONE 'UTC')::int AS hour")
    db.execute(
        "INSERT INTO app_config(key,value) VALUES ('health_warning_digest_hour_utc',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (db.jsonb((row["hour"] + 1) % 24),),
    )
    f.make_user(groups=["infra-admins"])
    finding = {
        "kind": "source_feed_empty",
        "subject": "tiny-board",
        "severity": "warning",
        "message": "No openings",
        "detail": {},
    }
    monkeypatch.setattr(health, "detect", lambda: [finding])
    monkeypatch.setattr(mail, "configured", lambda: True)
    sent = []
    monkeypatch.setattr(mail, "send_health_alert", lambda *args: sent.append(args))
    await health_task.handle_data_health(f.make_task("data_health"), {})
    assert sent == []
    assert (
        db.query_one("SELECT notified_at FROM health_alerts WHERE subject='tiny-board'")[
            "notified_at"
        ]
        is None
    )


@pytest.mark.asyncio
async def test_existing_critical_alert_retries_delivery(f, monkeypatch):
    f.make_user(groups=["infra-admins"])
    finding = {
        "kind": "ingest_failing",
        "subject": "broken-board",
        "severity": "critical",
        "message": "Feed failed",
        "detail": {},
    }
    health.record([finding])
    monkeypatch.setattr(health, "detect", lambda: [finding])
    monkeypatch.setattr(mail, "configured", lambda: True)
    sent = []
    monkeypatch.setattr(mail, "send_health_alert", lambda *args: sent.append(args))
    await health_task.handle_data_health(f.make_task("data_health"), {})
    assert len(sent) == 1
    assert (
        db.query_one("SELECT notified_at FROM health_alerts WHERE subject='broken-board'")[
            "notified_at"
        ]
        is not None
    )
