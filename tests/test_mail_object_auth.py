"""Object-level authorisation on the per-application mail routes.

Route-level auth being complete says nothing about object-level auth. This
repo has been here before: four per-job routes accepted any of 49k ids behind
correct authentication. These two took a nested id from the request and
verified only the PARENT.
"""

from __future__ import annotations

import datetime

import pytest

from api import db
from tests.conftest import _auth_headers


def _other_user_application(f) -> tuple[int, int, int]:
    """Someone else's application, message, match and event."""
    uid = f.make_user()
    app = db.query_one(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, 'Theirs', 'email') RETURNING id",
        (uid,),
    )
    msg = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
        "subject, sent_at, body_text) VALUES (%s, '<theirs@x>', 'gmail', 'hr@theirs.test', "
        "'Their private subject', %s, 'their private body') RETURNING id",
        (uid, datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)),
    )
    match = db.query_one(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s, %s, 'company_name', 'high') RETURNING id",
        (msg["id"], app["id"]),
    )
    event = db.query_one(
        "INSERT INTO email_events (message_id, kind, confidence) "
        "VALUES (%s, 'rejection', 'high') RETURNING id",
        (msg["id"],),
    )
    return app["id"], match["id"], event["id"]


@pytest.fixture
def mine(client, f):
    headers = _auth_headers("objauth-user", "obj@example.com", ["jobtracker-users-internal"])
    assert client.post("/v1/users/bootstrap", headers=headers).status_code == 200
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", ("objauth-user",))["id"]
    app = db.query_one(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, 'Mine', 'email') RETURNING id",
        (uid,),
    )
    return headers, uid, app["id"]


def test_reattach_refuses_a_match_from_another_application(mine, f):
    """Owning the application says nothing about owning the MATCH. Without the
    binding, the other user's message_id is appended to an application the
    caller does own - which puts that message's subject and body on the
    caller's pipeline page."""
    headers, _uid, my_app = mine
    _their_app, their_match, _their_event = _other_user_application(f)
    client = pytest.importorskip("fastapi.testclient").TestClient(
        __import__("api.app", fromlist=["app"]).app
    )

    resp = client.post(
        f"/v1/user/pipeline/{my_app}/matches/{their_match}/reattach",
        json={"note": "borrowed"},
        headers=headers,
    )
    assert resp.status_code == 404, resp.text
    leaked = db.query_one(
        "SELECT count(*) AS c FROM application_matches "
        "WHERE application_id = %s AND method = 'manual'",
        (my_app,),
    )
    assert leaked["c"] == 0, "another user's message must not be attachable"


def test_answering_a_suggestion_refuses_an_event_from_elsewhere(mine, f):
    """The suggestion list joins event -> message -> match -> application. The
    answer endpoint has to require the same relationship, or another user's
    event decides which status this application moves to."""
    headers, _uid, my_app = mine
    _their_app, _their_match, their_event = _other_user_application(f)
    client = pytest.importorskip("fastapi.testclient").TestClient(
        __import__("api.app", fromlist=["app"]).app
    )

    resp = client.post(
        f"/v1/user/suggestions/{my_app}/{their_event}",
        json={"response": "accepted"},
        headers=headers,
    )
    assert resp.status_code == 404, resp.text
    assert (
        db.query_one(
            "SELECT count(*) AS c FROM suggestion_responses WHERE application_id = %s", (my_app,)
        )["c"]
        == 0
    )
