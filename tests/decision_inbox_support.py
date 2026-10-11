from __future__ import annotations

import datetime

import pytest

from api import db
from tests import mail_log
from tests.conftest import _auth_headers

SENT = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


@pytest.fixture
def me(client):
    headers = _auth_headers("inbox-user", "inbox@example.com", ["jobtracker-users-internal"])
    assert client.post("/v1/users/bootstrap", headers=headers).status_code == 200
    row = db.query_one("SELECT id FROM users WHERE sub = %s", ("inbox-user",))
    assert row is not None
    return headers, row["id"]


def _msg(uid: int, mid: str, kind: str, company: str | None) -> int:
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
        "subject, sent_at, body_text) "
        "VALUES (%s, %s, 'gmail', 'hr@acme.test', 'Update', %s, 'body') RETURNING id",
        (uid, mid, SENT),
    )
    assert row is not None
    mail_log.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
        "VALUES (%s, %s, 'high', %s, 'gpt-5-nano')",
        (row["id"], kind, db.jsonb({"company": company})),
    )
    return row["id"]


def _app(uid: int, company: str = "Acme", job_id: int | None = None) -> int:
    row = db.query_one(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance, "
        "applied_at) VALUES (%s, %s, %s, 'Engineer', 'tracker', %s) RETURNING id",
        (uid, job_id, company, SENT - datetime.timedelta(days=30)),
    )
    assert row is not None
    return row["id"]


def _attach(message_id: int, application_id: int, method: str = "ats_company") -> int:
    """An attachment as the MATCHER writes one: no actor, which is the state
    every one of the 4,674 in production is in."""
    row = mail_log.query_one(
        "INSERT INTO application_matches (message_id, application_id, method, confidence, "
        "rationale) VALUES (%s, %s, %s, 'medium', 'single application at Acme') RETURNING id",
        (message_id, application_id, method),
    )
    assert row is not None
    return row["id"]


def _items(client, headers, kind: str | None = None) -> list[dict]:
    url = "/v1/user/resolve/queue?limit=200"
    if kind:
        url += f"&kind={kind}"
    resp = client.get(url, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _choices(item) -> dict:
    return {c["choice"]: c for c in item["choices"]}
