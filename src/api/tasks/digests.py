"""Daily email digest."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from api.tasks.runtime import _set_progress

logger = logging.getLogger("jobtracker_worker")


async def handle_send_digests(task_id: int, payload: dict[str, Any]) -> None:
    """Daily batched digest (never per-event: single-IP mail server, see
    homelab constraints). force+user_id sends the last day's rows regardless
    of digest state, used for template testing by admins."""
    import secrets as _secrets

    from api import mail

    if not mail.configured():
        _set_progress(task_id, 0, 0, "mail not configured")
        return
    force = bool(payload.get("force"))
    where_user = "AND u.id = %(only)s" if payload.get("user_id") else ""
    users = db.query(
        f"""
        SELECT u.id, u.email, s.digest_token, s.last_digest_at
        FROM users u JOIN user_settings s ON s.user_id = u.id
        WHERE (s.email_digest OR %(force)s) AND u.email LIKE '%%@%%' {where_user}
        """,
        {"force": force, "only": payload.get("user_id")},
    )
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
                {"uid": u["id"], "since": u["last_digest_at"]},
            )
            if not rows:
                continue
            token = u["digest_token"]
            if not token:
                token = _secrets.token_urlsafe(24)
                db.execute(
                    "UPDATE user_settings SET digest_token = %s WHERE user_id = %s",
                    (token, u["id"]),
                )
            await asyncio.to_thread(mail.send_digest, u["email"], rows, token)
            if not force:
                db.execute(
                    "UPDATE user_settings SET last_digest_at = now() WHERE user_id = %s",
                    (u["id"],),
                )
            sent += 1
        except Exception:
            logger.exception(f"digest failed for user {u['id']}")
    _set_progress(task_id, sent, len(users), "digests sent")
