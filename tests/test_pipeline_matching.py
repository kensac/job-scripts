"""Pipeline API behavior: matching."""

from __future__ import annotations

import datetime

from api import db
from tests import mail_log
from tests.pipeline_data import (
    insert_application,
    insert_board_job,
    insert_mail_event,
    insert_message_match,
    insert_takeout_message,
    next_message_suffix,
)


def test_candidates_lead_with_what_the_matcher_refused_to_choose(client, user_headers):
    """_by_company refuses when two applications at one employer are both
    plausible. Those rejected candidates are exactly what a person should see
    first - the system already knows the answer is one of them."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    insert_application(uid, company="Tesla", title="Frontend Engineer")
    insert_application(uid, company="Tesla", title="Autopilot Engineer")
    insert_application(uid, company="Unrelated Co", title="Engineer")
    mid = insert_takeout_message(uid)
    mail_log.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail) "
        "VALUES (%s,'rejection','high',%s)",
        (mid, db.jsonb({"company": "Tesla, Inc.", "role_title": "Engineer"})),
    )

    body = client.get(f"/v1/user/messages/{mid}/candidates", headers=user_headers).json()
    assert body["same_company_candidates"] == 2, "the count the matcher choked on"
    assert [a["reason"] for a in body["applications"][:2]] == [
        "same company as this mail",
        "same company as this mail",
    ]
    assert body["message"]["extracted_company"] == "Tesla, Inc."


def test_candidates_include_board_jobs_with_no_application_yet(client, user_headers):
    """'This belongs to a job I tracked but never recorded applying to' has
    nothing to attach to until an application exists."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    insert_board_job(uid, "Stripe", "Backend Engineer")
    mid = insert_takeout_message(uid)
    body = client.get(f"/v1/user/messages/{mid}/candidates?q=stripe", headers=user_headers).json()
    assert [j["company"] for j in body["board_jobs"]] == ["Stripe"]


def test_assigning_to_a_board_job_creates_the_application(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    job = insert_board_job(uid, "Stripe", "Backend Engineer")
    mid = insert_takeout_message(uid)
    insert_mail_event(mid, "offer")

    resp = client.post(
        f"/v1/user/messages/{mid}/assign", headers=user_headers, json={"job_id": job}
    )
    assert resp.status_code == 200
    app_id = resp.json()["application_id"]
    detail = client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()
    assert detail["company_name"] == "Stripe"
    assert detail["job_id"] == job
    assert detail["stage"] == "offer"


def test_assigning_to_a_new_company_never_invents_a_job(client, user_headers):
    """Mail predating the catalog has no posting and never will. An email does
    not get to create a jobs row."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    before = db.query_one("SELECT count(*) AS n FROM jobs")["n"]
    mid = insert_takeout_message(uid)
    insert_mail_event(mid, "rejection")

    resp = client.post(
        f"/v1/user/messages/{mid}/assign",
        headers=user_headers,
        json={"company_name": "Initech", "title": "Backend Intern"},
    )
    app_id = resp.json()["application_id"]
    detail = client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()
    assert detail["job_id"] is None
    assert detail["source_provenance"] == "manual"
    assert db.query_one("SELECT count(*) AS n FROM jobs")["n"] == before


def test_reassigning_moves_the_message_and_keeps_the_old_row(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    wrong = insert_application(uid, company="Wrong", title="Engineer")
    right = insert_application(uid, company="Right", title="Engineer")
    mid = insert_takeout_message(uid)
    insert_mail_event(mid, "offer")
    insert_message_match(mid, wrong)

    client.post(
        f"/v1/user/messages/{mid}/assign", headers=user_headers, json={"application_id": right}
    )
    assert (
        client.get(f"/v1/user/pipeline/{wrong}", headers=user_headers).json()["stage"] == "applied"
    )
    assert client.get(f"/v1/user/pipeline/{right}", headers=user_headers).json()["stage"] == "offer"
    assert (
        db.query_one("SELECT count(*) AS n FROM application_matches WHERE message_id = %s", (mid,))[
            "n"
        ]
        == 2
    )


def test_assigning_needs_a_target(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = insert_takeout_message(uid)
    assert (
        client.post(f"/v1/user/messages/{mid}/assign", headers=user_headers, json={}).status_code
        == 400
    )


def test_another_users_message_cannot_be_assigned(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = insert_takeout_message(uid)
    assert (
        client.get(f"/v1/user/messages/{mid}/candidates", headers=other_user_headers).status_code
        == 404
    )


def test_a_match_carries_what_it_was_decided_from(client, user_headers):
    """The rationale says what the matcher concluded. Evidence says what it
    concluded it FROM - and the extracted company is the thing tiers 2 and 3
    actually compared, so a wrong extraction is a wrong match and staring at
    the conclusion never reveals it."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Tesla", title="Frontend Engineer")
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, from_email, "
        "sent_at, body_text) VALUES (%s,%s,'takeout',%s,%s,%s,%s) RETURNING id",
        (
            uid,
            f"ev-{next_message_suffix()}",
            "Update on your application",
            "careers@tesla.com",
            datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC),
            # Long enough that excerpting genuinely saves something; a short
            # message comes back whole and would not exercise the centring.
            "Dear Kanishk,\n\n" + ("filler " * 200) + "We have decided not to move forward "
            "with your application to Tesla for the Frontend role. " + ("more " * 200),
        ),
    )["id"]
    mail_log.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
        "VALUES (%s,'rejection','high',%s,'gpt-5.6-luna')",
        (mid, db.jsonb({"company": "Tesla", "role_title": "Frontend Engineer"})),
    )
    insert_message_match(mid, app_id)

    detail = client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()
    ev = detail["matches"][0]["evidence"]
    assert ev["extracted_company"] == "Tesla"
    assert ev["classified_as"] == "rejection"
    assert ev["classifier_model"] == "gpt-5.6-luna"
    assert ev["from_domain"] == "tesla.com", "the one fact no model produced"
    # The WHOLE message, and where the company is mentioned in it. An excerpt
    # meant reading a fragment, clicking, and reading the same message again
    # from the top with no marker on the part that mattered.
    assert ev["mention"]["text"].startswith("Dear Kanishk")
    assert "not to move forward" in ev["mention"]["text"]
    assert ev["mention"]["term"] == "Tesla"
    assert ev["mention"]["text"][ev["mention"]["start"] : ev["mention"]["end"]] == "Tesla"


def test_the_whole_message_is_readable_without_leaving(client, user_headers):
    """Withholding the body would mean checking a decision this system made
    requires opening a mail client, which is the same as not being able to."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, from_email, "
        "sent_at, body_text) VALUES (%s,%s,'gmail','Offer','hr@initech.com',%s,%s) RETURNING id",
        (
            uid,
            f"ev-{next_message_suffix()}",
            datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC),
            "Full body here",
        ),
    )["id"]
    insert_mail_event(mid, "offer")

    body = client.get(f"/v1/user/messages/{mid}", headers=user_headers).json()
    assert body["body_text"] == "Full body here"
    assert body["from_email"] == "hr@initech.com"
    assert [e["kind"] for e in body["events"]] == ["offer"]


def test_another_users_message_body_is_not_readable(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = insert_takeout_message(uid)
    assert client.get(f"/v1/user/messages/{mid}", headers=other_user_headers).status_code == 404


def test_the_mention_says_when_the_company_is_not_in_the_body(client, user_headers):
    """The extracted company often does not appear verbatim - the classifier
    reads it from a signature or a logo. The excerpt still has to render."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Acme", title="Engineer")
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, from_email, "
        "sent_at, body_text) VALUES (%s,%s,'takeout','Update','no-reply@x.com',%s,%s) RETURNING id",
        (
            uid,
            f"ev-{next_message_suffix()}",
            datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC),
            "Thanks for applying.",
        ),
    )["id"]
    mail_log.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail) "
        "VALUES (%s,'acknowledgement','high',%s)",
        (mid, db.jsonb({"company": "Acme Corporation"})),
    )
    insert_message_match(mid, app_id)

    ev = client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()["matches"][0][
        "evidence"
    ]
    assert ev["mention"]["text"] == "Thanks for applying."
    assert ev["mention"]["start"] is None, "say the company is not in the body, do not imply it"
    assert ev["mention"]["term"] is None
