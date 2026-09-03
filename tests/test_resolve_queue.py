"""One queue, one verb set, declared by the server.

The contract the picker renders from: a row carries what it is, the evidence,
and every verb with whether it is available and what it would touch. A refused
verb stays visible with a short reason rather than disappearing.
"""

from __future__ import annotations

import datetime

import pytest

from api import db
from tests.conftest import _auth_headers


def _msg(uid: int, mid: str, kind: str, company: str | None, thread: str | None = None) -> int:
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, source, "
        "from_email, subject, sent_at, body_text) "
        "VALUES (%s, %s, %s, 'gmail', 'hr@acme.test', 'Update', %s, 'body') RETURNING id",
        (uid, mid, thread, datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)),
    )
    assert row is not None
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
        "VALUES (%s, %s, 'high', %s, 'gpt-5-nano')",
        (row["id"], kind, db.jsonb({"company": company})),
    )
    return row["id"]


@pytest.fixture
def me(client):
    headers = _auth_headers("resolve-user", "resolve@example.com", ["jobtracker-users-internal"])
    assert client.post("/v1/users/bootstrap", headers=headers).status_code == 200
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", ("resolve-user",))["id"]
    return headers, uid


def _choices(item) -> dict:
    return {c["choice"]: c for c in item["choices"]}


def test_the_queue_declares_every_verb_including_the_unavailable_ones(client, me):
    """A verb that is unavailable and says why teaches more than one that is
    silently absent - the picker greys it with the reason printed."""
    headers, uid = me
    _msg(uid, "<q1@x>", "rejection", "Acme")

    body = client.get("/v1/user/resolve/queue", headers=headers).json()
    assert body["total"] == 1
    choices = _choices(body["items"][0])
    assert set(choices) == {"assign_application", "not_an_application", "not_job_related"}
    assert choices["assign_application"]["eligible"] is False
    assert choices["assign_application"]["reason"] == "no application at this company yet"
    assert choices["not_an_application"]["eligible"] is True


def test_a_verb_that_moves_a_conversation_says_so_before_the_click(client, me):
    """Assign carries the whole thread, so the count belongs in the button
    rather than in the response afterwards."""
    headers, uid = me
    db.execute(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, 'Acme', 'tracker')",
        (uid,),
    )
    for i in range(3):
        _msg(uid, f"<t{i}@x>", "rejection", "Acme", thread="THREAD-1")

    body = client.get("/v1/user/resolve/queue", headers=headers).json()
    assign = _choices(body["items"][0])["assign_application"]
    assert assign["eligible"] is True
    assert assign["affects"] == {"messages": 3}


def test_a_single_message_verb_omits_affects(client, me):
    """Omission MEANS one. A verb that can reach further must send the field
    rather than drop it, or it wears a single-message costume."""
    headers, uid = me
    _msg(uid, "<solo@x>", "rejection", "Acme")

    body = client.get("/v1/user/resolve/queue", headers=headers).json()
    for choice in body["items"][0]["choices"]:
        assert "affects" not in choice


def test_a_deliberate_refusal_leaves_the_queue_and_a_failure_does_not(client, me):
    """`not_an_application` is not `unmatched`: deliberately attached to
    nothing versus looked and found nothing. Collapsing them has put correct
    decisions back into the queue on seven surfaces."""
    headers, uid = me
    mid = _msg(uid, "<ref@x>", "rejection", "Acme")

    assert client.get("/v1/user/resolve/queue", headers=headers).json()["total"] == 1
    resp = client.post(
        f"/v1/user/resolve/message:{mid}",
        json={"choice": "not_an_application"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert client.get("/v1/user/resolve/queue", headers=headers).json()["total"] == 0
    row = db.query_one(
        "SELECT method, actor_user_id FROM application_matches WHERE message_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (mid,),
    )
    assert row["method"] == "not_an_application"
    assert row["actor_user_id"] == uid


def test_not_job_related_retracts_the_match_as_well_as_the_kind(client, me):
    """An event saying this is not job mail cannot leave the message attached
    to a job. Retracting the kind without the match is how messages end up
    holding an application that their own current kind forbids."""
    headers, uid = me
    app = db.query_one(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, 'Acme', 'tracker') RETURNING id",
        (uid,),
    )
    mid = _msg(uid, "<njr@x>", "rejection", "Acme")
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s, %s, 'company_name', 'high')",
        (mid, app["id"]),
    )

    resp = client.post(
        f"/v1/user/resolve/message:{mid}", json={"choice": "not_job_related"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    event = db.query_one(
        "SELECT kind, actor_user_id FROM email_events WHERE message_id = %s ORDER BY id DESC "
        "LIMIT 1",
        (mid,),
    )
    assert event["kind"] == "not_job_related"
    assert event["actor_user_id"] == uid
    match = db.query_one(
        "SELECT application_id FROM application_matches WHERE message_id = %s ORDER BY id DESC "
        "LIMIT 1",
        (mid,),
    )
    assert match["application_id"] is None, "the match is retracted with the kind"


def test_assigning_needs_a_target_and_refuses_someone_elses(client, me, f):
    headers, uid = me
    mid = _msg(uid, "<tgt@x>", "rejection", "Acme")
    theirs = db.query_one(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, 'Acme', 'tracker') RETURNING id",
        (f.make_user(),),
    )

    assert (
        client.post(
            f"/v1/user/resolve/message:{mid}",
            json={"choice": "assign_application"},
            headers=headers,
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/v1/user/resolve/message:{mid}",
            json={"choice": "assign_application", "target": theirs["id"]},
            headers=headers,
        ).status_code
        == 404
    )


def test_one_user_cannot_resolve_anothers_item(client, me, f):
    headers, _uid = me
    mid = _msg(f.make_user(), "<other@x>", "rejection", "Acme")
    resp = client.post(
        f"/v1/user/resolve/message:{mid}",
        json={"choice": "not_an_application"},
        headers=headers,
    )
    assert resp.status_code == 404


def test_the_admin_queue_takes_the_owner_as_a_parameter(client, me, f):
    admin = _auth_headers("resolve-admin", "resadmin@example.com", ["infra-admins"])
    assert client.post("/v1/users/bootstrap", headers=admin).status_code == 200
    _headers, uid = me
    _msg(uid, "<adm@x>", "rejection", "Acme")

    body = client.get(f"/v1/admin/resolve/queue?user_id={uid}", headers=admin).json()
    assert body["total"] == 1
