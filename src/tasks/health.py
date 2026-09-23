"""Data-health detectors."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db, metrics
from tasks.runtime import set_progress

logger = logging.getLogger(__name__)


async def handle_data_health(task_id: int, payload: dict[str, Any]) -> None:
    """Persist findings independently of digest and incident notification policy."""
    from api import health

    run = health.detect()
    fresh = health.record(run)
    row = db.query_one("SELECT COUNT(*) AS n FROM health_alerts WHERE resolved_at IS NULL")
    open_count = int(row["n"]) if row else 0
    metrics.HEALTH_ALERTS.set(open_count)
    await asyncio.to_thread(_notify_pending)
    set_progress(
        task_id,
        open_count,
        open_count,
        f"{open_count} open, {len(fresh)} new" if open_count else "all clear",
    )


def _notify_pending() -> None:
    from api.health.notifications import due_alerts

    # One sender across workers; failed delivery leaves the original evidence
    # unacknowledged and eligible for the next health run.
    with db.transaction():
        locked = db.query_one(
            "SELECT pg_try_advisory_xact_lock(hashtext('health-notifications')) AS acquired"
        )
        if not locked or not locked["acquired"]:
            return
        pending = due_alerts()
        if pending:
            _notify(pending)


def _notify(fresh: list[dict[str, Any]]) -> None:
    """Acknowledge only actual delivery, never a digest delay or cooldown.

    SMTP has no transaction with Postgres. A crash after sending but before
    commit can repeat a message; marking it delivered first would lose it.
    """
    from api import mail

    ids = [a["id"] for a in fresh if a.get("id") is not None]

    if not mail.configured():
        # Silent until now: whether an alert is mailed depended on which host
        # claimed the task, and a host without SMTP skipped without a word.
        logger.error(
            "health alert NOT mailed: SMTP is not configured on this worker "
            "(%d new alert(s) affected, ids=%s)",
            len(fresh),
            ids,
        )
        return

    admins = db.query(
        "SELECT DISTINCT email FROM users WHERE email LIKE '%%@%%' AND 'infra-admins' = ANY(groups)"
    )
    if not admins:
        logger.error("health alert NOT mailed: no infra-admins with an address (ids=%s)", ids)
        return

    delivered = 0
    for a in admins:
        try:
            mail.send_health_alert(a["email"], fresh)
            delivered += 1
        except Exception:
            logger.exception("health alert mail failed")

    if not delivered:
        return
    db.execute(
        "UPDATE health_alerts SET notified_at = now() WHERE id = ANY(%s) AND notified_at IS NULL",
        (ids,),
    )
