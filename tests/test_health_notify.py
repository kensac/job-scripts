"""Whether an alert was delivered must outlive the container that sent it.

An alert is mailed once, when it opens; nothing retries and nothing
reconciles. The only evidence used to be a log line inside a worker
container, and containers are replaced on every roll - eleven on 2026-09-04.
The 17:00 batch_failed_whole alert cannot be answered for at all, because the
container that would have logged it was recreated 56 minutes later.
"""

from typing import Any

import pytest

from api import db
from api.tasks import health as health_task


@pytest.fixture
def alert_row():
    db.execute("DELETE FROM health_alerts WHERE subject = 'test-notify'")
    row = db.query_one(
        "INSERT INTO health_alerts (kind, subject, severity, message, detail) "
        "VALUES ('test_kind', 'test-notify', 'warning', 'msg', %s) RETURNING id",
        (db.jsonb({}),),
    )
    yield row["id"]
    db.execute("DELETE FROM health_alerts WHERE subject = 'test-notify'")


def _fresh(alert_id: int) -> list[dict[str, Any]]:
    return [{"id": alert_id, "severity": "warning", "message": "msg"}]


def _notified(alert_id: int):
    return db.query_one("SELECT notified_at FROM health_alerts WHERE id = %s", (alert_id,))[
        "notified_at"
    ]


def test_a_delivered_alert_is_recorded(alert_row, monkeypatch):
    monkeypatch.setattr("api.mail.configured", lambda: True)
    monkeypatch.setattr("api.mail.send_health_alert", lambda to, alerts: None)
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"email": "admin@example.com"}])
    health_task._notify(_fresh(alert_row))
    assert _notified(alert_row) is not None


def test_smtp_not_configured_leaves_it_unrecorded(alert_row, monkeypatch, caplog):
    monkeypatch.setattr("api.mail.configured", lambda: False)
    health_task._notify(_fresh(alert_row))
    assert _notified(alert_row) is None
    assert "SMTP is not configured" in caplog.text


def test_a_failed_send_leaves_it_unrecorded(alert_row, monkeypatch):
    def boom(to, alerts):
        raise RuntimeError("smtp refused")

    monkeypatch.setattr("api.mail.configured", lambda: True)
    monkeypatch.setattr("api.mail.send_health_alert", boom)
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"email": "admin@example.com"}])
    health_task._notify(_fresh(alert_row))
    assert _notified(alert_row) is None


def test_no_admins_leaves_it_unrecorded(alert_row, monkeypatch, caplog):
    monkeypatch.setattr("api.mail.configured", lambda: True)
    monkeypatch.setattr(db, "query", lambda *a, **k: [])
    health_task._notify(_fresh(alert_row))
    assert _notified(alert_row) is None
    assert "no infra-admins" in caplog.text
