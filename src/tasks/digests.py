"""Daily email digest."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db, user_settings
from tasks.runtime import set_progress

logger = logging.getLogger(__name__)


async def handle_send_digests(task_id: int, payload: dict[str, Any]) -> None:
    """Daily batched digest (never per-event: single-IP mail server, see
    homelab constraints). force+user_id sends the last day's rows regardless
    of digest state, used for template testing by admins."""
    from api import mail

    if not mail.configured():
        set_progress(task_id, 0, 0, "mail not configured")
        return
    force = bool(payload.get("force"))
    users = user_settings.digest_recipients(force=force, only=payload.get("user_id"))
    sent = 0
    for u in users:
        try:
            since_clause = (
                "uj.created_at > now() - interval '1 day'"
                if force
                else "uj.created_at > COALESCE(%(since)s, now() - interval '1 day')"
            )
            rows = db.query(
                f"""
                SELECT j.company, j.title, j.locations, j.comp_text
                FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
                WHERE uj.user_id = %(uid)s AND {since_clause}
                ORDER BY uj.created_at DESC
                """,
                {"uid": u.user_id, "since": u.last_digest_at},
            )
            if not rows:
                continue
            token = u.digest_token or user_settings.ensure_digest_token(u.user_id)
            await asyncio.to_thread(mail.send_digest, u.email, rows, token)
            if not force:
                user_settings.mark_digest_sent(u.user_id)
            sent += 1
        except Exception:
            logger.exception(f"digest failed for user {u.user_id}")
    set_progress(task_id, sent, len(users), "digests sent")
