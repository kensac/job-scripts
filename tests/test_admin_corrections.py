"""Admin correction tools over another user's mailbox.

The admin had exactly one affordance - a bare application-id field - which
requires knowing an id displayed nowhere, so in practice there was no
correction available at all. These are the user-side tools, scoped to the
message's OWNER rather than to the caller, with the actor recorded.
"""

from __future__ import annotations

import datetime

import pytest

from api import db
from tests.conftest import _auth_headers


def _owner_with_message(f) -> tuple[int, int, int]:
    uid = f.make_user()
    app = db.query_one(
        "INSERT INTO applications (user_id, company_name, title, source_provenance, applied_at) "
        "VALUES (%s, 'Acme', 'Engineer', 'email', %s) RETURNING id",
        (uid, datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)),
    )
    msg = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, subject, "
        "sent_at, body_text) VALUES (%s, '<own@x>', 'gmail', 'hr@acme.test', 'Update', %s, 'hi') "
        "RETURNING id",
        (uid, datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC)),
    )
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
        "VALUES (%s, 'acknowledgement', 'high', %s, 'gpt-5-nano')",
        (msg["id"], db.jsonb({"company": "Acme", "role_title": "Engineer"})),
    )
    return uid, app["id"], msg["id"]


@pytest.fixture
def admin(client):
    headers = _auth_headers("corr-admin", "corradmin@example.com", ["infra-admins"])
    assert client.post("/v1/users/bootstrap", headers=headers).status_code == 200
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", ("corr-admin",))["id"]
    return headers, uid


def test_the_admin_gets_the_same_candidate_picker_the_user_does(client, admin, f):
    """A bare id field requires knowing an id that is displayed nowhere, so the
    only correction on offer was no correction."""
    headers, _ = admin
    _uid, app_id, msg_id = _owner_with_message(f)

    resp = client.get(f"/v1/admin/mail/{msg_id}/candidates", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [a["id"] for a in body["applications"]] == [app_id]
    assert body["applications"][0]["reason"] == "same company as this mail"
    assert body["message"]["extracted_company"] == "Acme"


def test_candidates_are_the_owners_not_the_admins(client, admin, f):
    """Scoped to whoever owns the message. Ranking the admin's own
    applications would be a picker that can never contain the right answer."""
    headers, admin_id = admin
    db.execute(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, 'Acme', 'email')",
        (admin_id,),
    )
    _uid, app_id, msg_id = _owner_with_message(f)

    body = client.get(f"/v1/admin/mail/{msg_id}/candidates", headers=headers).json()
    assert [a["id"] for a in body["applications"]] == [app_id]


def test_an_admin_correction_records_who_made_it(client, admin, f):
    """`model IS NULL` says a human corrected it and cannot say WHICH human.
    Both logs are append-only, so attribution not written at the time can
    never be reconstructed."""
    headers, admin_id = admin
    _uid, _app_id, msg_id = _owner_with_message(f)

    resp = client.post(
        f"/v1/admin/mail/{msg_id}/classify",
        json={"kind": "rejection", "note": "obviously a rejection"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    row = db.query_one(
        "SELECT kind, model, actor_user_id FROM email_events WHERE message_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (msg_id,),
    )
    assert row["kind"] == "rejection"
    assert row["model"] is None
    assert row["actor_user_id"] == admin_id


def test_the_owner_is_told_a_correction_was_an_administrators(client, admin, f):
    """A correction someone cannot see is one they cannot question. The same
    actor id reads as "you" to the owner and "administrator" to anyone else,
    derived rather than stored twice."""
    _headers, admin_id = admin
    uid, app_id, msg_id = _owner_with_message(f)
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence, "
        "actor_user_id) VALUES (%s, %s, 'manual', 'high', %s)",
        (msg_id, app_id, admin_id),
    )

    from api.routers.mail import _corrected_by

    assert _corrected_by(admin_id, uid) == "administrator"
    assert _corrected_by(uid, uid) == "you"
    # Four answers, not three. A machine wrote it, or a person did and there
    # is no record of which - every human correction predating this column
    # lands in the second case, and the logs are append-only so it can never
    # be resolved. Rendering that as "nobody corrected it" would be a lie.
    assert _corrected_by(None, uid, model="gpt-5-nano") == "model"
    assert _corrected_by(None, uid) == "unknown"


def test_reverting_restores_the_models_answer_and_records_the_actor(client, admin, f):
    headers, admin_id = admin
    _uid, _app_id, msg_id = _owner_with_message(f)
    client.post(f"/v1/admin/mail/{msg_id}/classify", json={"kind": "rejection"}, headers=headers)

    resp = client.post(f"/v1/admin/mail/{msg_id}/classify/revert", headers=headers)
    assert resp.status_code == 200, resp.text
    row = db.query_one(
        "SELECT kind, model, actor_user_id FROM email_events WHERE message_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (msg_id,),
    )
    assert row["kind"] == "acknowledgement", "the model's answer is restored"
    assert row["model"] == "gpt-5-nano"
    assert row["actor_user_id"] == admin_id, "who asked for the restore is still recorded"


def test_an_ordinary_user_cannot_reach_the_admin_tools(client, f):
    headers = _auth_headers("plain-user", "plain@example.com", ["jobtracker-users-internal"])
    assert client.post("/v1/users/bootstrap", headers=headers).status_code == 200
    _uid, _app_id, msg_id = _owner_with_message(f)

    assert client.get(f"/v1/admin/mail/{msg_id}/candidates", headers=headers).status_code == 403
    assert (
        client.post(
            f"/v1/admin/mail/{msg_id}/classify", json={"kind": "rejection"}, headers=headers
        ).status_code
        == 403
    )


def test_an_unknown_message_is_a_404_not_a_crash(client, admin):
    headers, _ = admin
    assert client.get("/v1/admin/mail/999999/candidates", headers=headers).status_code == 404
    assert (
        client.post(
            "/v1/admin/mail/999999/classify", json={"kind": "rejection"}, headers=headers
        ).status_code
        == 404
    )


def test_an_admin_refusal_is_not_filed_as_a_matcher_failure(client, admin, f):
    """`not_an_application` is not `unmatched`: deliberately attached to
    nothing versus looked and found nothing. Recording every admin no-match as
    method='manual' with a null application put it back in the queue of things
    needing attention, because the unmatched cut reads exactly that shape as a
    failure - so the admin took the correct action and nothing said otherwise.
    """
    headers, admin_id = admin
    _uid, _app_id, msg_id = _owner_with_message(f)

    resp = client.post(
        f"/v1/admin/mail/{msg_id}/match",
        json={"application_id": None, "outcome": "not_an_application"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    row = db.query_one(
        "SELECT application_id, method, confidence, actor_user_id FROM application_matches "
        "WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (msg_id,),
    )
    assert row["application_id"] is None
    assert row["method"] == "not_an_application", "a refusal, not a failure"
    assert row["actor_user_id"] == admin_id


def test_a_plain_no_match_still_reads_as_a_failure_to_find_one(client, admin, f):
    """The weaker claim is the default. A no-match with no reason given is
    'looked and found nothing', which is what a caller that predates the field
    means and the safe reading of what it says."""
    headers, _ = admin
    _uid, _app_id, msg_id = _owner_with_message(f)

    resp = client.post(
        f"/v1/admin/mail/{msg_id}/match", json={"application_id": None}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    row = db.query_one(
        "SELECT method FROM application_matches WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (msg_id,),
    )
    assert row["method"] == "manual"
