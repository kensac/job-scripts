"""Pipeline API behavior: mail actions."""

from __future__ import annotations

from api import db
from api.mail import pipeline as mail_pipeline
from tests.pipeline_data import (
    insert_action_request,
    insert_application,
    insert_board_application,
    insert_gmail_message,
    insert_message_match,
)


def test_a_user_can_see_their_own_mail_without_being_an_admin(client, user_headers):
    """Reading your own inbox should not require the permission to read
    everyone's. /admin/mail is the debug view: it spans every user and is
    gated behind infra-admin."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    insert_gmail_message(uid, subject="Thanks for applying", kind="acknowledgement", company="Acme")

    body = client.get("/v1/user/mail", headers=user_headers).json()
    assert body["total"] == 1
    row = body["messages"][0]
    assert row["kind"] == "acknowledgement"
    assert row["extracted_company"] == "Acme"
    assert row["application_id"] is None


def test_a_users_mail_list_never_shows_another_users(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    insert_gmail_message(uid, subject="Private", kind="offer")
    assert client.get("/v1/user/mail", headers=other_user_headers).json()["total"] == 0


def test_mail_can_be_filtered_by_whether_it_reached_an_application(client, user_headers):
    """'What arrived and where did it go' is the question this answers, so the
    unmatched half has to be reachable - that is where a correction starts."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    hit = insert_gmail_message(uid, kind="rejection", company="Acme")
    insert_message_match(hit, app_id)
    insert_gmail_message(uid, kind="rejection", company="Nobody")

    assert client.get("/v1/user/mail?matched=true", headers=user_headers).json()["total"] == 1
    assert client.get("/v1/user/mail?matched=false", headers=user_headers).json()["total"] == 1
    only = client.get("/v1/user/mail?matched=true", headers=user_headers).json()["messages"][0]
    assert only["company_name"] == "Acme", "say WHICH application it reached"


def test_mail_filters_by_kind_and_search(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    insert_gmail_message(uid, subject="Offer letter", sender="hr@initech.com", kind="offer")
    insert_gmail_message(uid, subject="Rejected", sender="no-reply@acme.com", kind="rejection")

    assert client.get("/v1/user/mail?kind=offer", headers=user_headers).json()["total"] == 1
    assert client.get("/v1/user/mail?q=initech", headers=user_headers).json()["total"] == 1


def test_a_rejection_is_suggested_not_applied(client, user_headers):
    """user_jobs.status is what the user typed. A system that silently rewrites
    it stops being trustworthy at the moment it is most confident."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    job, app_id = insert_board_application(uid, "Acme", "Engineer")
    mid = insert_gmail_message(uid, subject="An update from Acme", kind="rejection", company="Acme")
    insert_message_match(mid, app_id)

    body = client.get("/v1/user/suggestions", headers=user_headers).json()
    assert body["total"] == 1
    s = body["suggestions"][0]
    assert s["board_status"] == "Application Submitted"
    assert s["suggested_status"] == "Rejected"
    assert s["evidence"]["from_domain"] is not None, "a suggestion he cannot check is faith"

    row = db.query_one("SELECT status, person_touched_at FROM user_jobs WHERE job_id = %s", (job,))
    assert row["status"] == "Application Submitted", "nothing moves until he says so"
    assert row["person_touched_at"] is None


def test_accepting_moves_the_board(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    job, app_id = insert_board_application(uid, "Acme", "Engineer")
    mid = insert_gmail_message(uid, kind="rejection", company="Acme")
    insert_message_match(mid, app_id)
    ev = db.query_one("SELECT id FROM email_events WHERE message_id = %s", (mid,))["id"]

    resp = client.post(
        f"/v1/user/suggestions/{app_id}/{ev}", headers=user_headers, json={"response": "accepted"}
    )
    assert resp.status_code == 200
    row = db.query_one("SELECT status, person_touched_at FROM user_jobs WHERE job_id = %s", (job,))
    assert row["status"] == "Rejected"
    assert row["person_touched_at"] is not None
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 0


def test_dismissing_silences_the_evidence_not_the_question(client, user_headers):
    """A dismissal keyed on the event means a LATER rejection from the same
    company gets asked again - which is what makes dismissing safe rather than
    a decision he can never revisit."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    job, app_id = insert_board_application(uid, "Acme", "Engineer")
    first = insert_gmail_message(uid, kind="rejection", company="Acme")
    insert_message_match(first, app_id)
    ev = db.query_one("SELECT id FROM email_events WHERE message_id = %s", (first,))["id"]

    client.post(
        f"/v1/user/suggestions/{app_id}/{ev}", headers=user_headers, json={"response": "dismissed"}
    )
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 0
    assert db.query_one("SELECT status FROM user_jobs WHERE job_id = %s", (job,))["status"] == (
        "Application Submitted"
    ), "dismissing changes nothing"

    later = insert_gmail_message(uid, kind="rejection", company="Acme")
    insert_message_match(later, app_id)
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 1


def test_nothing_is_suggested_once_he_has_moved_it_himself(client, user_headers):
    """If the board already says Rejected, the mail is confirming what he knows
    rather than telling him something."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _job, app_id = insert_board_application(uid, "Acme", "Engineer", status="Rejected")
    mid = insert_gmail_message(uid, kind="rejection", company="Acme")
    insert_message_match(mid, app_id)
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 0


def test_an_acknowledgement_suggests_nothing(client, user_headers):
    """It means the application is alive, which the board already says."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _job, app_id = insert_board_application(uid, "Acme", "Engineer")
    mid = insert_gmail_message(uid, kind="acknowledgement", company="Acme")
    insert_message_match(mid, app_id)
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 0


def test_another_users_suggestion_cannot_be_answered(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _job, app_id = insert_board_application(uid, "Private", "Engineer")
    mid = insert_gmail_message(uid, kind="rejection", company="Private")
    insert_message_match(mid, app_id)
    ev = db.query_one("SELECT id FROM email_events WHERE message_id = %s", (mid,))["id"]
    resp = client.post(
        f"/v1/user/suggestions/{app_id}/{ev}",
        headers=other_user_headers,
        json={"response": "accepted"},
    )
    assert resp.status_code == 404


def test_an_action_nothing_can_close_is_closeable_by_the_person(client, user_headers):
    """respond_to_offer settles only on a rejection, so accepting an offer,
    declining it or signing never closes it: 146 open in production and not one
    has ever resolved. For those kinds a person is the only producer, exactly
    as the board is the only producer of `withdrawn`."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    action = insert_action_request(uid, app_id)

    resp = client.post(
        f"/v1/user/actions/{action}/resolve", headers=user_headers, json={"note": "signed"}
    )
    assert resp.status_code == 200
    item = mail_pipeline.action_item(uid, action)
    assert item is not None and item.resolved_at is not None
    assert item.resolution == "signed"


def test_a_closed_ask_stays_closed_and_reopens_only_by_answer(client, user_headers):
    """Nothing recomputes an ask any more, so nothing can undo a person's
    answer except another answer."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    action = insert_action_request(uid, app_id)
    client.post(f"/v1/user/actions/{action}/resolve", headers=user_headers, json={})
    assert mail_pipeline.action_items(uid, app_id, open_only=True) == []
    resp = client.post(f"/v1/user/actions/{action}/reopen", headers=user_headers, json={})
    assert resp.status_code == 200
    assert [i.id for i in mail_pipeline.action_items(uid, app_id, open_only=True)] == [action]


def test_reopening_is_refused_when_an_email_settled_it(client, user_headers):
    """That is a fact about the mail rather than a decision the person made,
    and reopening it would change nothing: the event still settles it."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    action = insert_action_request(uid, app_id, kind="complete_assessment")
    later = insert_gmail_message(uid, kind="acknowledgement", company="Acme")
    insert_message_match(later, app_id)
    resp = client.post(f"/v1/user/actions/{action}/reopen", headers=user_headers, json={})
    assert resp.status_code == 409


def test_another_users_action_cannot_be_closed(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Private", title="Engineer")
    action = insert_action_request(uid, app_id)
    assert (
        client.post(
            f"/v1/user/actions/{action}/resolve", headers=other_user_headers, json={}
        ).status_code
        == 404
    )
