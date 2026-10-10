"""The daily digest announces postings that newly became visible (phase 2b)."""

from __future__ import annotations

import pytest

from api import db, mail
from tasks import digests


@pytest.mark.asyncio
async def test_digest_announces_new_board_members_not_new_working_set_rows(f, monkeypatch):
    sent: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(mail, "configured", lambda: True)
    monkeypatch.setattr(mail, "send_digest", lambda to, rows, token: sent.append((to, rows)))

    uid = f.make_user(email="digest@example.test")
    db.execute(
        "INSERT INTO user_settings (user_id, email_digest, last_digest_at) "
        "VALUES (%s, true, now() - interval '1 hour')",
        (uid,),
    )
    joined = f.make_job(title="Joined since the last digest")
    earlier = f.make_job(title="Joined before the last digest")
    picked = f.make_job(title="Picked by a filter, not visible")
    db.execute(
        "INSERT INTO board_visible (user_id, job_id, computed_at) VALUES "
        "(%s, %s, now()), (%s, %s, now() - interval '2 hours')",
        (uid, joined, uid, earlier),
    )
    db.execute("INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s)", (uid, picked))
    db.execute("INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)", (uid, picked))

    task_id = f.make_task("send_digests", {}, status="running")
    await digests.handle_send_digests(task_id, {})

    assert [(to, [r["title"] for r in rows]) for to, rows in sent] == [
        ("digest@example.test", ["Joined since the last digest"])
    ]
    stamped = db.query_one(
        "SELECT last_digest_at > now() - interval '1 minute' AS fresh "
        "FROM user_settings WHERE user_id = %s",
        (uid,),
    )
    assert stamped == {"fresh": True}
