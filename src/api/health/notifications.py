"""Notification eligibility is separate from whether a condition exists."""

from datetime import UTC, datetime, timedelta
from typing import Any

from api import db


def incident_key(alert: dict[str, Any]) -> tuple[str, str]:
    if alert["kind"] == "task_progress_stalled":
        return alert["kind"], (alert.get("detail") or {}).get("kind") or alert["subject"]
    return alert["kind"], alert["subject"]


def select_due(
    pending: list[dict[str, Any]],
    delivered: list[dict[str, Any]],
    now: datetime,
    digest_hour: int,
    repeat_hours: int,
) -> list[dict[str, Any]]:
    selected = []
    seen = set()
    for alert in sorted(
        pending, key=lambda a: (a["severity"] == "critical", a["first_seen"]), reverse=True
    ):
        key = incident_key(alert)
        if key in seen:
            continue
        # New task IDs do not make an unchanged handler stall a new incident.
        seen.add(key)
        due = alert["first_seen"]
        for previous in delivered:
            if incident_key(previous) != key:
                continue
            if alert["severity"] == "critical" and previous["severity"] != "critical":
                continue
            due = max(due, previous["notified_at"] + timedelta(hours=repeat_hours))
        if alert["severity"] != "critical":
            slot = due.astimezone(UTC).replace(hour=digest_hour, minute=0, second=0, microsecond=0)
            due = slot if slot >= due else slot + timedelta(days=1)
        if due <= now:
            selected.append({**alert, "notification_due_at": due})
    return selected


def due_alerts() -> list[dict[str, Any]]:
    now = db.query_one("SELECT now() AS now")["now"]
    repeat_hours = int(db.get_config("health_notification_repeat_hours"))
    pending = db.query(
        "SELECT * FROM health_alerts WHERE resolved_at IS NULL AND notified_at IS NULL"
    )
    delivered = db.query(
        "SELECT kind,subject,severity,detail,notified_at FROM health_alerts "
        "WHERE notified_at > now()-make_interval(hours => %s)",
        (repeat_hours,),
    )
    return select_due(
        pending,
        delivered,
        now,
        int(db.get_config("health_warning_digest_hour_utc")),
        repeat_hours,
    )
