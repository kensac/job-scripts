import pytest

from api import db, health, mail
from tasks import health as health_task


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
