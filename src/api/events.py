from __future__ import annotations

import logging
import os
from typing import Any

import requests

from api import db

logger = logging.getLogger(__name__)

CENTRIFUGO_API_URL = os.environ.get("CENTRIFUGO_API_URL", "").rstrip("/")
CENTRIFUGO_API_KEY = os.environ.get("CENTRIFUGO_API_KEY", "")

TASKS_CHANNEL = "jobtracker:tasks"


def _publish(channel: str, data: dict[str, Any]) -> None:
    """Fire-and-forget realtime publish; the app must never depend on it."""
    if not CENTRIFUGO_API_URL or not CENTRIFUGO_API_KEY:
        return
    try:
        requests.post(
            f"{CENTRIFUGO_API_URL}/publish",
            json={"channel": channel, "data": data},
            headers={"X-API-Key": CENTRIFUGO_API_KEY},
            timeout=2,
        )
    except Exception:
        logger.debug("centrifugo publish failed", exc_info=True)


def publish_task(task_id: int) -> None:
    """Push a task's current state to the admin channel and, when the task
    belongs to a user, to that user's channel."""
    if not CENTRIFUGO_API_URL or not CENTRIFUGO_API_KEY:
        return
    row = db.query_one(
        "SELECT id, kind, status, attempts, progress, error, payload, "
        "created_at, started_at, finished_at FROM tasks WHERE id = %s",
        (task_id,),
    )
    if not row:
        return
    payload = row.get("payload") or {}
    event = {
        "type": "task",
        "task": {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "attempts": row["attempts"],
            "progress": row["progress"],
            "error": row["error"],
            "source": payload.get("source"),
            "user_id": payload.get("user_id"),
            # The subject the task is about, so a view can match "a task
            # about MY job finished" from the event alone. Matching by a
            # task id the page remembered from its own POST fails the moment
            # the page is reloaded or the task was started from another tab:
            # the application panel did that and sat on a finished draft.
            "job_id": payload.get("job_id"),
            "filter_id": payload.get("filter_id"),
            "created_at": str(row["created_at"]),
            "started_at": str(row["started_at"]) if row["started_at"] else None,
            "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
        },
    }
    _publish(TASKS_CHANNEL, event)
    user_id: int | None = payload.get("user_id")
    if user_id is not None:
        _publish(f"jobtracker:user.{user_id}", event)
