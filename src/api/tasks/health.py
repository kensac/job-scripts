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
    from api import health

    found = health.detect()
    fresh = health.record(found)
    metrics.HEALTH_ALERTS.set(len(found))
    if fresh:
        await asyncio.to_thread(_notify, fresh)
    _set_progress(
        task_id,
        len(found),
        len(found),
        f"{len(found)} open, {len(fresh)} new" if found else "all clear",
    )


def _notify(fresh: list[dict[str, Any]]) -> None:
    """Mail the newly-opened alerts and record on the row whether that worked.

    An alert is mailed once, when it opens. Nothing retries and nothing
    reconciles, so a send that does not happen is a notification lost for the
    life of the condition. Until now the only evidence either way was a log
    line in the worker container - and a container is replaced on every roll,
    of which there were eleven on 2026-09-04. The 17:00 alert that started
    this cannot be answered for at all: the container that would have logged
    it was recreated 56 minutes later.

    So the outcome goes on the row. notified_at set means it left the box;
    still NULL on an open alert means nobody was told, and that is now a
    question SQL can answer after the fact rather than one that depends on
    reading a log before the next deploy.
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
