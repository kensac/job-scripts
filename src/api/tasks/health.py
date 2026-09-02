"""Data-health detectors."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db, metrics
from api.tasks.runtime import _set_progress

logger = logging.getLogger("jobtracker_worker")


async def handle_data_health(task_id: int, payload: dict[str, Any]) -> None:
    """Watches for upstream changes that would otherwise surface as a pile of
    quietly misclassified jobs weeks later. Alerts fire once per condition and
    auto-resolve, so the mail stays worth reading."""
    from api import health, mail

    found = health.detect()
    fresh = health.record(found)
    metrics.HEALTH_ALERTS.set(len(found))
    if fresh and mail.configured():
        admins = db.query(
            "SELECT DISTINCT email FROM users WHERE email LIKE '%%@%%' "
            "AND 'infra-admins' = ANY(groups)"
        )
        for a in admins:
            try:
                await asyncio.to_thread(mail.send_health_alert, a["email"], fresh)
            except Exception:
                logger.exception("health alert mail failed")
    _set_progress(
        task_id,
        len(found),
        len(found),
        f"{len(found)} open, {len(fresh)} new" if found else "all clear",
    )
