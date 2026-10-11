"""Pipeline API behavior: corrections."""

from __future__ import annotations

from api import db
from tests import mail_log
from tests.pipeline_data import (
    insert_application,
    insert_gmail_message,
    insert_gmail_thread,
    insert_message_match,
    next_message_suffix,
)


def test_assigning_one_message_carries_its_conversation(client, user_headers):
    """A person correcting one message of a thread means the thread. Making
    them do it five times is the chore this system exists to remove."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    ids = insert_gmail_thread(uid, "thread-x", 4)

    body = client.post(
        f"/v1/user/messages/{ids[0]}/assign",
        headers=user_headers,
        json={"application_id": app_id},
    ).json()
    assert body["messages_assigned"] == 4

    detail = client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()
    assert {m["message_id"] for m in detail["matches"] if m["in_force"]} == set(ids)


def test_a_thread_of_one_assigns_one(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    ids = insert_gmail_thread(uid, "thread-solo", 1)
    body = client.post(
        f"/v1/user/messages/{ids[0]}/assign",
        headers=user_headers,
        json={"application_id": app_id},
    ).json()
    assert body["messages_assigned"] == 1


def test_threadless_mail_never_drags_a_lookalike_along(client, user_headers):
    """The measured danger: grouping threadless mail by normalised subject and
    sender would treat 49 'thank you for applying!' messages from myworkday.com
    as one conversation, when they are 49 different employers. Only the
    provider's own thread id counts."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    first = insert_gmail_message(
        uid, subject="Thank you for applying!", sender="x@myworkday.com", kind="acknowledgement"
    )
    insert_gmail_message(
        uid, subject="Thank you for applying!", sender="x@myworkday.com", kind="acknowledgement"
    )

    body = client.post(
        f"/v1/user/messages/{first}/assign",
        headers=user_headers,
        json={"application_id": app_id},
    ).json()
    assert body["messages_assigned"] == 1, "identical subject and sender is not a conversation"


def test_the_whole_thread_can_be_declined(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    ids = insert_gmail_thread(uid, "thread-y", 3)
    body = client.post(
        f"/v1/user/messages/{ids[0]}/assign",
        headers=user_headers,
        json={"application_id": app_id, "whole_thread": False},
    ).json()
    assert body["messages_assigned"] == 1


def test_assigning_a_reply_takes_the_message_that_started_the_thread(client, user_headers):
    """provider_thread_id is the first References entry - the Message-ID of the
    message that STARTED the thread. So replies carry it and the root does not,
    because a first message has no References. A 2026-10-11 production audit found
    1,113 messages that originated a thread without belonging to it. Assigning a
    reply moved its siblings and silently left the original behind."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")

    root = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, source, "
        "subject, from_email, sent_at) VALUES (%s,'<root@acme>',NULL,'takeout','Your application',"
        "'a@acme.com',now()) RETURNING id",
        (uid,),
    )["id"]
    reply = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, source, "
        "subject, from_email, sent_at) VALUES (%s,'<r1@acme>','<root@acme>','takeout',"
        "'Re: Your application','a@acme.com',now()) RETURNING id",
        (uid,),
    )["id"]

    body = client.post(
        f"/v1/user/messages/{reply}/assign",
        headers=user_headers,
        json={"application_id": app_id},
    ).json()
    assert body["messages_assigned"] == 2, "the message that started it belongs to it"

    detail = client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()
    assert {m["message_id"] for m in detail["matches"] if m["in_force"]} == {root, reply}


def test_a_message_with_no_thread_and_no_replies_stays_alone(client, user_headers):
    """Coalescing to the message's own id must not become a way to group
    unrelated mail - that is the failure that made subject grouping unsafe."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    alone = insert_gmail_message(uid, subject="Thanks for applying", kind="acknowledgement")
    insert_gmail_message(uid, subject="Thanks for applying", kind="acknowledgement")

    body = client.post(
        f"/v1/user/messages/{alone}/assign",
        headers=user_headers,
        json={"application_id": app_id},
    ).json()
    assert body["messages_assigned"] == 1


def test_a_person_can_say_what_a_message_actually_is(client, user_headers):
    """Every other correction fixes the MATCH. Stage is derived from KINDS, so
    a rejection read as an acknowledgement silently moves an application - and
    the only affordance on offer was detaching a match that was correct."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    mid = insert_gmail_message(
        uid, subject="An update from Acme", kind="acknowledgement", company="Acme"
    )
    insert_message_match(mid, app_id)
    assert client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()["stage"] == (
        "acknowledged"
    )

    resp = client.post(
        f"/v1/user/messages/{mid}/classify",
        headers=user_headers,
        json={"kind": "rejection", "note": "this was a rejection"},
    )
    assert resp.status_code == 200
    assert client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()["stage"] == (
        "rejected"
    ), "the stage recomputes with nobody restating it"


def test_a_correction_supersedes_rather_than_erases(client, user_headers):
    """Events are append-only and the latest wins, so the model's wrong answer
    stays visible in the history. A correction that erases its own cause cannot
    be reviewed."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = insert_gmail_message(uid, kind="acknowledgement", company="Acme")
    client.post(
        f"/v1/user/messages/{mid}/classify", headers=user_headers, json={"kind": "rejection"}
    )
    rows = db.query(
        "SELECT kind, model, detail FROM email_events WHERE message_id = %s ORDER BY id", (mid,)
    )
    assert [r["kind"] for r in rows] == ["acknowledgement", "rejection"]
    assert rows[-1]["model"] is None, "no model is how a human correction is told from a model's"
    assert rows[-1]["detail"]["corrected_by_user"] is True


def test_a_correction_carries_the_extraction_forward(client, user_headers):
    """Fixing the kind is not also asserting the company was wrong, and
    blanking it would break the matching that already worked."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = insert_gmail_message(uid, kind="acknowledgement", company="Acme")
    client.post(
        f"/v1/user/messages/{mid}/classify", headers=user_headers, json={"kind": "rejection"}
    )
    latest = db.query_one(
        "SELECT detail FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1", (mid,)
    )
    assert latest["detail"]["company"] == "Acme"


def test_an_unknown_kind_is_refused(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = insert_gmail_message(uid, kind="acknowledgement")
    assert (
        client.post(
            f"/v1/user/messages/{mid}/classify",
            headers=user_headers,
            json={"kind": "sounds_promising"},
        ).status_code
        == 400
    )


def test_another_users_message_cannot_be_reclassified(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = insert_gmail_message(uid, kind="acknowledgement")
    assert (
        client.post(
            f"/v1/user/messages/{mid}/classify",
            headers=other_user_headers,
            json={"kind": "rejection"},
        ).status_code
        == 404
    )


def test_a_correction_reports_which_applications_moved(client, user_headers):
    """The point of the correction is that the STAGE moves, and it can move an
    application the person is not looking at - so the ids come back rather than
    an ok."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    mid = insert_gmail_message(uid, kind="acknowledgement", company="Acme")
    insert_message_match(mid, app_id)

    body = client.post(
        f"/v1/user/messages/{mid}/classify", headers=user_headers, json={"kind": "rejection"}
    ).json()
    assert body["affected_application_ids"] == [app_id]


def test_a_correction_can_be_reverted_to_what_the_model_said(client, user_headers):
    """Another append, not a delete: a mis-correction has to be recoverable and
    the log still has to show that both happened."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    mid = insert_gmail_message(uid, company="Acme")
    # A MODEL-authored event, which is what a revert restores. The helper
    # writes model=NULL, and NULL is exactly how a human correction is told
    # apart from a model's answer.
    mail_log.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
        "VALUES (%s,'acknowledgement','high',%s,'gpt-5.6-luna')",
        (mid, db.jsonb({"company": "Acme"})),
    )
    insert_message_match(mid, app_id)

    client.post(f"/v1/user/messages/{mid}/classify", headers=user_headers, json={"kind": "offer"})
    assert (
        client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()["stage"] == "offer"
    )

    resp = client.post(f"/v1/user/messages/{mid}/classify/revert", headers=user_headers)
    assert resp.status_code == 200
    assert client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()["stage"] == (
        "acknowledged"
    )
    kinds = [
        r["kind"]
        for r in db.query("SELECT kind FROM email_events WHERE message_id = %s ORDER BY id", (mid,))
    ]
    assert kinds == ["acknowledgement", "offer", "acknowledgement"], "all three are visible"


def test_reverting_with_nothing_to_restore_is_refused(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
        "VALUES (%s,%s,'gmail','s',now()) RETURNING id",
        (uid, f"norev-{next_message_suffix()}"),
    )["id"]
    mail_log.execute(
        "INSERT INTO email_events (message_id, kind, confidence, model) "
        "VALUES (%s,'rejection','high',NULL)",
        (mid,),
    )
    assert (
        client.post(f"/v1/user/messages/{mid}/classify/revert", headers=user_headers).status_code
        == 409
    )


def test_the_kind_vocabulary_is_served_not_copied(client, user_headers):
    """The client kept this list in two places, and it drifts the moment a kind
    is added - the same failure as the stage vocabulary, which had a terminal
    state the frontend did not know about."""
    kinds = client.get("/v1/user/message-kinds", headers=user_headers).json()["kinds"]
    assert "rejection" in kinds and "not_job_related" in kinds
