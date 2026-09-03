"""The pipeline API: derived stage, paging over a derivation, and honesty
about which match is in force."""

from __future__ import annotations

import datetime
import itertools

from api import db

_seq = itertools.count(1)


def _app(uid, *, company, title=None, prov="tracker", job_id=None, applied=None):
    return db.query_one(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance, "
        "applied_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (uid, job_id, company, title, prov, applied),
    )["id"]


def _msg(uid):
    return db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
        "VALUES (%s,%s,'takeout','s',%s) RETURNING id",
        (uid, f"p-{next(_seq)}", datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)),
    )["id"]


def _event(mid, kind):
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s,%s,'high')", (mid, kind)
    )


def _match(mid, app_id, method="ats_company"):
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s,%s,%s,'high')",
        (mid, app_id, method),
    )


def test_summary_and_list_agree_on_every_stage(client, user_headers):
    """The counts endpoint exists so the browser never sums stages itself. It
    only helps if both come from the same derivation."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    for kind in ("acknowledgement", "rejection", "offer"):
        app_id = _app(uid, company=f"C{kind}", title="Engineer")
        mid = _msg(uid)
        _event(mid, kind)
        _match(mid, app_id)

    summary = client.get("/v1/user/pipeline/summary", headers=user_headers).json()
    assert summary["counts"] == {"acknowledged": 1, "rejected": 1, "offer": 1}
    assert summary["total"] == 3

    listed = client.get(
        "/v1/user/pipeline?include_closed=true&limit=500", headers=user_headers
    ).json()
    from collections import Counter

    assert Counter(a["stage"] for a in listed["applications"]) == summary["counts"]


def test_paging_reports_a_total_over_the_whole_derivation(client, user_headers):
    """Stage cannot be paged in SQL because it does not exist there. The total
    still has to describe every application, not the page."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    for i in range(7):
        _app(uid, company=f"Co{i}", title="Engineer")
    body = client.get("/v1/user/pipeline?limit=3&offset=0", headers=user_headers).json()
    assert len(body["applications"]) == 3
    assert body["total"] == 7
    assert body["has_more"] is True
    last = client.get("/v1/user/pipeline?limit=3&offset=6", headers=user_headers).json()
    assert len(last["applications"]) == 1
    assert last["has_more"] is False


def test_a_rematched_message_is_not_in_force_on_the_old_application(client, user_headers):
    """The bug this test exists for: computing in_force with a window over one
    application's rows makes a message that was rematched AWAY still look
    current on the application it left."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    old_app = _app(uid, company="Wrong", title="Engineer")
    new_app = _app(uid, company="Right", title="Engineer")
    mid = _msg(uid)
    _event(mid, "rejection")
    _match(mid, old_app)
    _match(mid, new_app)

    detail = client.get(f"/v1/user/pipeline/{old_app}", headers=user_headers).json()
    # The old application shows the message's whole history, including where it
    # went: its own row is superseded, and the row in force points elsewhere.
    # A match that simply vanished would leave nothing to explain the stage.
    mine = [m for m in detail["matches"] if m["application_id"] == old_app]
    assert [m["in_force"] for m in mine] == [False]
    assert any(m["in_force"] and m["application_id"] == new_app for m in detail["matches"])
    assert detail["stage"] == "applied", "a retracted match contributes no events"

    moved = client.get(f"/v1/user/pipeline/{new_app}", headers=user_headers).json()
    assert [m["in_force"] for m in moved["matches"] if m["application_id"] == new_app] == [True]
    assert moved["stage"] == "rejected"


def test_another_users_application_is_not_readable(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Private", title="Engineer")
    assert client.get(f"/v1/user/pipeline/{app_id}", headers=other_user_headers).status_code == 404


def test_an_application_with_no_job_is_a_real_row(client, user_headers):
    """job_id is nullable by design - the posting was never in the catalog and
    never will be. Those must not be hidden."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _app(uid, company="Initech", title="Backend", prov="email")
    body = client.get("/v1/user/pipeline", headers=user_headers).json()
    assert len(body["applications"]) == 1
    assert body["applications"][0]["job_id"] is None
    assert body["applications"][0]["source_provenance"] == "email"


def test_filters_narrow_the_derivation(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    tracked = _app(uid, company="Tracked", title="Engineer")
    _app(uid, company="Derived", title="Engineer", prov="email")
    mid = _msg(uid)
    _event(mid, "offer")
    _match(mid, tracked, method="company_title")

    assert (
        client.get("/v1/user/pipeline?provenance=email", headers=user_headers).json()["total"] == 1
    )
    assert client.get("/v1/user/pipeline?stage=offer", headers=user_headers).json()["total"] == 1
    assert (
        client.get("/v1/user/pipeline?tier=company_title", headers=user_headers).json()["total"]
        == 1
    )
    assert client.get("/v1/user/pipeline?q=derived", headers=user_headers).json()["total"] == 1


def test_detaching_a_message_recomputes_the_stage(client, user_headers):
    """The derived-stage design paying off: the correction is one append and
    nobody restates the stage."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    mid = _msg(uid)
    _event(mid, "offer")
    _match(mid, app_id)
    match_id = db.query_one("SELECT id FROM application_matches WHERE message_id = %s", (mid,))[
        "id"
    ]

    assert (
        client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()["stage"] == "offer"
    )

    resp = client.post(
        f"/v1/user/pipeline/{app_id}/matches/{match_id}/detach",
        headers=user_headers,
        json={"note": "not this role"},
    )
    assert resp.status_code == 200

    after = client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()
    assert after["stage"] == "applied"
    assert (
        db.query_one("SELECT count(*) AS n FROM application_matches WHERE message_id = %s", (mid,))[
            "n"
        ]
        == 2
    ), "the wrong match stays in the history; a correction that erases its cause cannot be reviewed"


def test_a_detach_is_undone_by_reattaching(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    mid = _msg(uid)
    _event(mid, "offer")
    _match(mid, app_id)
    match_id = db.query_one("SELECT id FROM application_matches WHERE message_id = %s", (mid,))[
        "id"
    ]

    client.post(
        f"/v1/user/pipeline/{app_id}/matches/{match_id}/detach", headers=user_headers, json={}
    )
    client.post(
        f"/v1/user/pipeline/{app_id}/matches/{match_id}/reattach", headers=user_headers, json={}
    )
    assert (
        client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()["stage"] == "offer"
    )


def test_a_tracker_application_cannot_be_dismissed(client, user_headers):
    """It exists because the user entered it. Mail evidence did not create it,
    so no mail correction may remove it."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer", prov="tracker")
    resp = client.post(f"/v1/user/pipeline/{app_id}/dismiss", headers=user_headers, json={})
    assert resp.status_code == 409
    assert (
        db.query_one("SELECT dismissed_at FROM applications WHERE id = %s", (app_id,))[
            "dismissed_at"
        ]
        is None
    )


def test_a_dismissed_application_is_counted_not_hidden(client, user_headers):
    """A total that silently shrinks with nothing explaining why is the exact
    failure this system keeps producing."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    keep = _app(uid, company="Real", title="Engineer", prov="email")
    drop = _app(uid, company="CMPSC 311", title="LA", prov="email")

    client.post(
        f"/v1/user/pipeline/{drop}/dismiss", headers=user_headers, json={"note": "coursework"}
    )

    summary = client.get("/v1/user/pipeline/summary", headers=user_headers).json()
    assert summary["total"] == 1
    assert summary["dismissed"] == 1

    listed = client.get("/v1/user/pipeline", headers=user_headers).json()
    assert [a["id"] for a in listed["applications"]] == [keep]

    review = client.get("/v1/user/pipeline?stage=dismissed", headers=user_headers).json()
    assert [a["id"] for a in review["applications"]] == [drop]
    assert review["applications"][0]["dismissed_reason"] == "coursework"

    client.post(f"/v1/user/pipeline/{drop}/restore", headers=user_headers, json={})
    assert client.get("/v1/user/pipeline/summary", headers=user_headers).json()["dismissed"] == 0


def _job_on_board(uid, company, title, status="Application Submitted"):
    job = db.query_one(
        "INSERT INTO jobs (url, raw_url, source, company, title, active) "
        "VALUES (%s,%s,'fulltime',%s,%s,TRUE) RETURNING id",
        (f"https://x/{next(_seq)}", f"https://x/{next(_seq)}", company, title),
    )["id"]
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s,%s,%s)", (uid, job, status)
    )
    return job


def test_candidates_lead_with_what_the_matcher_refused_to_choose(client, user_headers):
    """_by_company refuses when two applications at one employer are both
    plausible. Those rejected candidates are exactly what a person should see
    first - the system already knows the answer is one of them."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _app(uid, company="Tesla", title="Frontend Engineer")
    _app(uid, company="Tesla", title="Autopilot Engineer")
    _app(uid, company="Unrelated Co", title="Engineer")
    mid = _msg(uid)
    db.execute(
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
    _job_on_board(uid, "Stripe", "Backend Engineer")
    mid = _msg(uid)
    body = client.get(f"/v1/user/messages/{mid}/candidates?q=stripe", headers=user_headers).json()
    assert [j["company"] for j in body["board_jobs"]] == ["Stripe"]


def test_assigning_to_a_board_job_creates_the_application(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    job = _job_on_board(uid, "Stripe", "Backend Engineer")
    mid = _msg(uid)
    _event(mid, "offer")

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
    mid = _msg(uid)
    _event(mid, "rejection")

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
    wrong = _app(uid, company="Wrong", title="Engineer")
    right = _app(uid, company="Right", title="Engineer")
    mid = _msg(uid)
    _event(mid, "offer")
    _match(mid, wrong)

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
    mid = _msg(uid)
    assert (
        client.post(f"/v1/user/messages/{mid}/assign", headers=user_headers, json={}).status_code
        == 400
    )


def test_another_users_message_cannot_be_assigned(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = _msg(uid)
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
    app_id = _app(uid, company="Tesla", title="Frontend Engineer")
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, from_email, "
        "sent_at, body_text) VALUES (%s,%s,'takeout',%s,%s,%s,%s) RETURNING id",
        (
            uid,
            f"ev-{next(_seq)}",
            "Update on your application",
            "careers@tesla.com",
            datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC),
            # Long enough that excerpting genuinely saves something; a short
            # message comes back whole and would not exercise the centring.
            "Dear Kanishk,\n\n" + ("filler " * 200) + "We have decided not to move forward "
            "with your application to Tesla for the Frontend role. " + ("more " * 200),
        ),
    )["id"]
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
        "VALUES (%s,'rejection','high',%s,'gpt-5.6-luna')",
        (mid, db.jsonb({"company": "Tesla", "role_title": "Frontend Engineer"})),
    )
    _match(mid, app_id)

    detail = client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()
    ev = detail["matches"][0]["evidence"]
    assert ev["extracted_company"] == "Tesla"
    assert ev["classified_as"] == "rejection"
    assert ev["classifier_model"] == "gpt-5.6-luna"
    assert ev["from_domain"] == "tesla.com", "the one fact no model produced"
    # Centred on the company mention, not the greeting: an email opens with a
    # salutation and a logo, and the sentence that decided this is in the
    # middle. Only for a message long enough that excerpting saves anything -
    # a short mail comes back whole, because two thirds of a short message
    # plus a link to the rest is worse than the message.
    assert ev["snippet"]["is_whole_message"] is False
    assert ev["snippet"]["centred_on"] == "Tesla"
    assert "not to move forward" in ev["snippet"]["text"]
    assert "Dear Kanishk" not in ev["snippet"]["text"]


def test_the_whole_message_is_readable_without_leaving(client, user_headers):
    """Withholding the body would mean checking a decision this system made
    requires opening a mail client, which is the same as not being able to."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, from_email, "
        "sent_at, body_text) VALUES (%s,%s,'gmail','Offer','hr@initech.com',%s,%s) RETURNING id",
        (
            uid,
            f"ev-{next(_seq)}",
            datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC),
            "Full body here",
        ),
    )["id"]
    _event(mid, "offer")

    body = client.get(f"/v1/user/messages/{mid}", headers=user_headers).json()
    assert body["body_text"] == "Full body here"
    assert body["from_email"] == "hr@initech.com"
    assert [e["kind"] for e in body["events"]] == ["offer"]


def test_another_users_message_body_is_not_readable(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = _msg(uid)
    assert client.get(f"/v1/user/messages/{mid}", headers=other_user_headers).status_code == 404


def test_a_snippet_falls_back_when_the_company_is_not_in_the_body(client, user_headers):
    """The extracted company often does not appear verbatim - the classifier
    reads it from a signature or a logo. The excerpt still has to render."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, from_email, "
        "sent_at, body_text) VALUES (%s,%s,'takeout','Update','no-reply@x.com',%s,%s) RETURNING id",
        (
            uid,
            f"ev-{next(_seq)}",
            datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC),
            "Thanks for applying.",
        ),
    )["id"]
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail) "
        "VALUES (%s,'acknowledgement','high',%s)",
        (mid, db.jsonb({"company": "Acme Corporation"})),
    )
    _match(mid, app_id)

    ev = client.get(f"/v1/user/pipeline/{app_id}", headers=user_headers).json()["matches"][0][
        "evidence"
    ]
    assert ev["snippet"]["text"] == "Thanks for applying."
    assert ev["snippet"]["centred_on"] is None, "say so rather than implying it was found"


def _mail(uid, *, subject="s", sender="a@b.com", kind=None, company=None):
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, from_email, "
        "sent_at) VALUES (%s,%s,'gmail',%s,%s,now()) RETURNING id",
        (uid, f"um-{next(_seq)}", subject, sender),
    )["id"]
    if kind:
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail) "
            "VALUES (%s,%s,'high',%s)",
            (mid, kind, db.jsonb({"company": company} if company else {})),
        )
    return mid


def test_a_user_can_see_their_own_mail_without_being_an_admin(client, user_headers):
    """Reading your own inbox should not require the permission to read
    everyone's. /admin/mail is the debug view: it spans every user and is
    gated behind infra-admin."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _mail(uid, subject="Thanks for applying", kind="acknowledgement", company="Acme")

    body = client.get("/v1/user/mail", headers=user_headers).json()
    assert body["total"] == 1
    row = body["messages"][0]
    assert row["kind"] == "acknowledgement"
    assert row["extracted_company"] == "Acme"
    assert row["application_id"] is None


def test_a_users_mail_list_never_shows_another_users(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _mail(uid, subject="Private", kind="offer")
    assert client.get("/v1/user/mail", headers=other_user_headers).json()["total"] == 0


def test_mail_can_be_filtered_by_whether_it_reached_an_application(client, user_headers):
    """'What arrived and where did it go' is the question this answers, so the
    unmatched half has to be reachable - that is where a correction starts."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    hit = _mail(uid, kind="rejection", company="Acme")
    _match(hit, app_id)
    _mail(uid, kind="rejection", company="Nobody")

    assert client.get("/v1/user/mail?matched=true", headers=user_headers).json()["total"] == 1
    assert client.get("/v1/user/mail?matched=false", headers=user_headers).json()["total"] == 1
    only = client.get("/v1/user/mail?matched=true", headers=user_headers).json()["messages"][0]
    assert only["company_name"] == "Acme", "say WHICH application it reached"


def test_mail_filters_by_kind_and_search(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _mail(uid, subject="Offer letter", sender="hr@initech.com", kind="offer")
    _mail(uid, subject="Rejected", sender="no-reply@acme.com", kind="rejection")

    assert client.get("/v1/user/mail?kind=offer", headers=user_headers).json()["total"] == 1
    assert client.get("/v1/user/mail?q=initech", headers=user_headers).json()["total"] == 1


def _board(uid, company, title, status="Application Submitted"):
    job = db.query_one(
        "INSERT INTO jobs (url, raw_url, source, company, title, active) "
        "VALUES (%s,%s,'fulltime',%s,%s,TRUE) RETURNING id",
        (f"https://s/{next(_seq)}", f"https://s/{next(_seq)}", company, title),
    )["id"]
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status, date_applied) VALUES (%s,%s,%s,now())",
        (uid, job, status),
    )
    app_id = db.query_one(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance, "
        "applied_at) VALUES (%s,%s,%s,%s,'tracker',now()) RETURNING id",
        (uid, job, company, title),
    )["id"]
    return job, app_id


def test_a_rejection_is_suggested_not_applied(client, user_headers):
    """user_jobs.status is what the user typed. A system that silently rewrites
    it stops being trustworthy at the moment it is most confident."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    job, app_id = _board(uid, "Acme", "Engineer")
    mid = _mail(uid, subject="An update from Acme", kind="rejection", company="Acme")
    _match(mid, app_id)

    body = client.get("/v1/user/suggestions", headers=user_headers).json()
    assert body["total"] == 1
    s = body["suggestions"][0]
    assert s["board_status"] == "Application Submitted"
    assert s["suggested_status"] == "Rejected"
    assert s["evidence"]["from_domain"] is not None, "a suggestion he cannot check is faith"

    assert db.query_one("SELECT status FROM user_jobs WHERE job_id = %s", (job,))["status"] == (
        "Application Submitted"
    ), "nothing moves until he says so"


def test_accepting_moves_the_board(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    job, app_id = _board(uid, "Acme", "Engineer")
    mid = _mail(uid, kind="rejection", company="Acme")
    _match(mid, app_id)
    ev = db.query_one("SELECT id FROM email_events WHERE message_id = %s", (mid,))["id"]

    resp = client.post(
        f"/v1/user/suggestions/{app_id}/{ev}", headers=user_headers, json={"response": "accepted"}
    )
    assert resp.status_code == 200
    assert db.query_one("SELECT status FROM user_jobs WHERE job_id = %s", (job,))["status"] == (
        "Rejected"
    )
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 0


def test_dismissing_silences_the_evidence_not_the_question(client, user_headers):
    """A dismissal keyed on the event means a LATER rejection from the same
    company gets asked again - which is what makes dismissing safe rather than
    a decision he can never revisit."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    job, app_id = _board(uid, "Acme", "Engineer")
    first = _mail(uid, kind="rejection", company="Acme")
    _match(first, app_id)
    ev = db.query_one("SELECT id FROM email_events WHERE message_id = %s", (first,))["id"]

    client.post(
        f"/v1/user/suggestions/{app_id}/{ev}", headers=user_headers, json={"response": "dismissed"}
    )
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 0
    assert db.query_one("SELECT status FROM user_jobs WHERE job_id = %s", (job,))["status"] == (
        "Application Submitted"
    ), "dismissing changes nothing"

    later = _mail(uid, kind="rejection", company="Acme")
    _match(later, app_id)
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 1


def test_nothing_is_suggested_once_he_has_moved_it_himself(client, user_headers):
    """If the board already says Rejected, the mail is confirming what he knows
    rather than telling him something."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _job, app_id = _board(uid, "Acme", "Engineer", status="Rejected")
    mid = _mail(uid, kind="rejection", company="Acme")
    _match(mid, app_id)
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 0


def test_an_acknowledgement_suggests_nothing(client, user_headers):
    """It means the application is alive, which the board already says."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _job, app_id = _board(uid, "Acme", "Engineer")
    mid = _mail(uid, kind="acknowledgement", company="Acme")
    _match(mid, app_id)
    assert client.get("/v1/user/suggestions", headers=user_headers).json()["total"] == 0


def test_another_users_suggestion_cannot_be_answered(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _job, app_id = _board(uid, "Private", "Engineer")
    mid = _mail(uid, kind="rejection", company="Private")
    _match(mid, app_id)
    ev = db.query_one("SELECT id FROM email_events WHERE message_id = %s", (mid,))["id"]
    resp = client.post(
        f"/v1/user/suggestions/{app_id}/{ev}",
        headers=other_user_headers,
        json={"response": "accepted"},
    )
    assert resp.status_code == 404


def _action(uid, app_id, kind="respond_to_offer", event_id=None):
    return db.query_one(
        "INSERT INTO action_items (user_id, application_id, event_id, kind) "
        "VALUES (%s,%s,%s,%s) RETURNING id",
        (uid, app_id, event_id, kind),
    )["id"]


def test_an_action_nothing_can_close_is_closeable_by_the_person(client, user_headers):
    """respond_to_offer settles only on a rejection, so accepting an offer,
    declining it or signing never closes it: 146 open in production and not one
    has ever resolved. For those kinds a person is the only producer, exactly
    as the board is the only producer of `withdrawn`."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    action = _action(uid, app_id)

    resp = client.post(
        f"/v1/user/actions/{action}/resolve", headers=user_headers, json={"note": "signed"}
    )
    assert resp.status_code == 200
    row = db.query_one("SELECT resolved_at, resolution FROM action_items WHERE id = %s", (action,))
    assert row["resolved_at"] is not None
    assert row["resolution"] == "signed"


def test_recomputing_does_not_reopen_what_the_person_closed(client, user_headers):
    """sync_action_items runs on every pass. A manual resolution that the next
    recomputation undoes is not a resolution."""
    from api.mail_pipeline import sync_action_items

    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    mid = _mail(uid, kind="offer", company="Acme")
    _match(mid, app_id)
    sync_action_items(app_id)

    action = db.query_one("SELECT id FROM action_items WHERE application_id = %s", (app_id,))["id"]
    client.post(f"/v1/user/actions/{action}/resolve", headers=user_headers, json={})
    sync_action_items(app_id)

    assert (
        db.query_one(
            "SELECT count(*) AS n FROM action_items WHERE application_id = %s AND resolved_at IS NULL",
            (app_id,),
        )["n"]
        == 0
    )


def test_reopening_is_refused_when_an_email_settled_it(client, user_headers):
    """That is a fact about the mail rather than a decision the person made,
    and reopening it would only have it close again on the next pass."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    mid = _mail(uid, kind="acknowledgement", company="Acme")
    settling = db.query_one("SELECT id FROM email_events WHERE message_id = %s", (mid,))["id"]
    action = _action(uid, app_id, kind="complete_assessment")
    db.execute(
        "UPDATE action_items SET resolved_at = now(), resolved_by_event_id = %s WHERE id = %s",
        (settling, action),
    )
    resp = client.post(f"/v1/user/actions/{action}/reopen", headers=user_headers, json={})
    assert resp.status_code == 409


def test_another_users_action_cannot_be_closed(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Private", title="Engineer")
    action = _action(uid, app_id)
    assert (
        client.post(
            f"/v1/user/actions/{action}/resolve", headers=other_user_headers, json={}
        ).status_code
        == 404
    )


def _threaded(uid, thread, n, kind="rejection", company="Acme"):
    ids = []
    for i in range(n):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, "
            "source, subject, from_email, sent_at) "
            "VALUES (%s,%s,%s,'gmail',%s,'a@acme.com',now()) RETURNING id",
            (uid, f"th-{next(_seq)}", thread, f"msg {i}"),
        )["id"]
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail) "
            "VALUES (%s,%s,'high',%s)",
            (mid, kind, db.jsonb({"company": company})),
        )
        ids.append(mid)
    return ids


def test_assigning_one_message_carries_its_conversation(client, user_headers):
    """A person correcting one message of a thread means the thread. Making
    them do it five times is the chore this system exists to remove."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    ids = _threaded(uid, "thread-x", 4)

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
    app_id = _app(uid, company="Acme", title="Engineer")
    ids = _threaded(uid, "thread-solo", 1)
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
    app_id = _app(uid, company="Acme", title="Engineer")
    first = _mail(
        uid, subject="Thank you for applying!", sender="x@myworkday.com", kind="acknowledgement"
    )
    _mail(uid, subject="Thank you for applying!", sender="x@myworkday.com", kind="acknowledgement")

    body = client.post(
        f"/v1/user/messages/{first}/assign",
        headers=user_headers,
        json={"application_id": app_id},
    ).json()
    assert body["messages_assigned"] == 1, "identical subject and sender is not a conversation"


def test_the_whole_thread_can_be_declined(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    ids = _threaded(uid, "thread-y", 3)
    body = client.post(
        f"/v1/user/messages/{ids[0]}/assign",
        headers=user_headers,
        json={"application_id": app_id, "whole_thread": False},
    ).json()
    assert body["messages_assigned"] == 1


def test_assigning_a_reply_takes_the_message_that_started_the_thread(client, user_headers):
    """provider_thread_id is the first References entry - the Message-ID of the
    message that STARTED the thread. So replies carry it and the root does not,
    because a first message has no References. 1,113 messages in production are
    the origin of a thread that exists without being part of it, so assigning a
    reply moved its siblings and silently left the original behind."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")

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
    app_id = _app(uid, company="Acme", title="Engineer")
    alone = _mail(uid, subject="Thanks for applying", kind="acknowledgement")
    _mail(uid, subject="Thanks for applying", kind="acknowledgement")

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
    app_id = _app(uid, company="Acme", title="Engineer")
    mid = _mail(uid, subject="An update from Acme", kind="acknowledgement", company="Acme")
    _match(mid, app_id)
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
    mid = _mail(uid, kind="acknowledgement", company="Acme")
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
    mid = _mail(uid, kind="acknowledgement", company="Acme")
    client.post(
        f"/v1/user/messages/{mid}/classify", headers=user_headers, json={"kind": "rejection"}
    )
    latest = db.query_one(
        "SELECT detail FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1", (mid,)
    )
    assert latest["detail"]["company"] == "Acme"


def test_an_unknown_kind_is_refused(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = _mail(uid, kind="acknowledgement")
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
    mid = _mail(uid, kind="acknowledgement")
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
    app_id = _app(uid, company="Acme", title="Engineer")
    mid = _mail(uid, kind="acknowledgement", company="Acme")
    _match(mid, app_id)

    body = client.post(
        f"/v1/user/messages/{mid}/classify", headers=user_headers, json={"kind": "rejection"}
    ).json()
    assert body["affected_application_ids"] == [app_id]


def test_a_correction_can_be_reverted_to_what_the_model_said(client, user_headers):
    """Another append, not a delete: a mis-correction has to be recoverable and
    the log still has to show that both happened."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Acme", title="Engineer")
    mid = _mail(uid, company="Acme")
    # A MODEL-authored event, which is what a revert restores. The helper
    # writes model=NULL, and NULL is exactly how a human correction is told
    # apart from a model's answer.
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
        "VALUES (%s,'acknowledgement','high',%s,'gpt-5.6-luna')",
        (mid, db.jsonb({"company": "Acme"})),
    )
    _match(mid, app_id)

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
        (uid, f"norev-{next(_seq)}"),
    )["id"]
    db.execute(
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


def test_mail_can_be_asked_for_one_applications_messages(client, user_headers):
    """Without it an application can only link to a company text search, which
    returns a DIFFERENT set and would quietly lie about being the messages
    behind this application."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mine = _app(uid, company="Acme", title="Engineer")
    other = _app(uid, company="Acme", title="Other")
    a = _mail(uid, kind="rejection", company="Acme")
    b = _mail(uid, kind="rejection", company="Acme")
    _match(a, mine)
    _match(b, other)

    body = client.get(f"/v1/user/mail?application_id={mine}", headers=user_headers).json()
    assert [m["id"] for m in body["messages"]] == [a]


def test_the_pipeline_filters_on_whether_there_is_evidence(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    heard = _app(uid, company="Heard", title="Engineer")
    _app(uid, company="Silent", title="Engineer")
    mid = _mail(uid, kind="acknowledgement", company="Heard")
    _match(mid, heard)

    assert client.get("/v1/user/pipeline?evidence=true", headers=user_headers).json()["total"] == 1
    assert client.get("/v1/user/pipeline?evidence=false", headers=user_headers).json()["total"] == 1


def test_a_lens_is_a_set_of_stages_not_one(client, user_headers):
    """Every view worth having is multi-valued - "waiting" is applied AND
    acknowledged, "over" is rejected AND closed AND withdrawn. A single-valued
    filter makes a lens either impossible or a client-side re-derivation of a
    set the server already knows."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    for company, kind in (("A", "acknowledgement"), ("B", "rejection"), ("C", "offer")):
        app_id = _app(uid, company=company, title="Engineer")
        mid = _mail(uid, kind=kind, company=company)
        _match(mid, app_id)

    over = client.get(
        "/v1/user/pipeline?stage=rejected,closed,withdrawn&include_closed=true",
        headers=user_headers,
    ).json()
    assert over["total"] == 1
    live = client.get("/v1/user/pipeline?stage=interviewing,assessment,offer", headers=user_headers)
    assert live.json()["total"] == 1


def test_mail_kinds_compose_and_carry_their_counts(client, user_headers):
    """A tab's number and its contents come from the same predicate, so they
    cannot disagree - and without the counts a tab either shows none or costs a
    request each to display a number the server already had."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _mail(uid, kind="rejection", company="A")
    _mail(uid, kind="offer", company="B")
    _mail(uid, kind="not_job_related")

    replies = client.get("/v1/user/mail?kind=rejection,offer", headers=user_headers).json()
    assert replies["total"] == 2
    assert replies["by_kind"] == {"rejection": 1, "offer": 1}

    everything = client.get("/v1/user/mail", headers=user_headers).json()
    assert everything["by_kind"]["not_job_related"] == 1


def test_the_pipeline_sorts_on_the_set_not_the_page(client, user_headers):
    """A client sorting what it was handed sorts one page of a filtered whole
    and reads as though it sorted everything, which is a quiet lie rather than
    a missing feature. Longest-silence-first IS the waiting question."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    old = _app(uid, company="Old", title="Engineer")
    recent = _app(uid, company="Recent", title="Engineer")
    for app_id, when in ((old, "2024-01-01"), (recent, "2026-01-01")):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
            "VALUES (%s,%s,'gmail','s',%s) RETURNING id",
            (uid, f"srt-{next(_seq)}", when),
        )["id"]
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s,'acknowledgement','high')",
            (mid,),
        )
        _match(mid, app_id)

    quietest = client.get(
        "/v1/user/pipeline?sort=last_event_at&dir=asc", headers=user_headers
    ).json()
    assert quietest["applications"][0]["id"] == old


def test_a_thread_reads_as_a_conversation_including_its_first_message(client, user_headers):
    """Mail is a flat list and the unit a person thinks in is the exchange:
    19,995 messages sit in conversations of more than one, so roughly a third
    of the corpus is shown out of its context.

    The root is included because the thread key coalesces to the message's own
    id - a reply carries the starter's Message-ID and the starter carries
    nothing, so keying on the provider field alone leaves the original out of
    its own conversation."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    root = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, source, "
        "subject, from_email, sent_at) VALUES (%s,'<r@acme>',NULL,'takeout','Your application',"
        "'a@acme.com','2025-06-01') RETURNING id",
        (uid,),
    )["id"]
    reply = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, source, "
        "subject, from_email, sent_at) VALUES (%s,'<r2@acme>','<r@acme>','takeout',"
        "'Re: Your application','a@acme.com','2025-06-02') RETURNING id",
        (uid,),
    )["id"]

    body = client.get(f"/v1/user/messages/{reply}/thread", headers=user_headers).json()
    assert [m["id"] for m in body["messages"]] == [root, reply], "oldest first, root included"
    assert body["truncated"] is False


def test_a_thread_says_when_it_was_cut(client, user_headers):
    """The longest key in this corpus holds 474 messages - a mailing list
    reusing a thread id rather than a conversation. A thread silently cut reads
    as a thread that ended."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    ids = _threaded(uid, "long-thread", 5)
    body = client.get(f"/v1/user/messages/{ids[0]}/thread?limit=2", headers=user_headers).json()
    assert len(body["messages"]) == 2
    assert body["truncated"] is True


def test_a_message_with_no_conversation_is_a_thread_of_one(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    alone = _mail(uid, subject="Standalone", kind="acknowledgement")
    body = client.get(f"/v1/user/messages/{alone}/thread", headers=user_headers).json()
    assert [m["id"] for m in body["messages"]] == [alone]


def test_another_users_thread_is_not_readable(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = _mail(uid, kind="rejection")
    assert (
        client.get(f"/v1/user/messages/{mid}/thread", headers=other_user_headers).status_code == 404
    )


def test_threads_list_as_conversations_not_messages(client, user_headers):
    """Grouping a page of messages client-side produces partial threads at the
    page boundaries - a conversation cut in half by pagination reads as a
    conversation that is half that long, which is a lie exactly where nobody
    looks for one."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    _threaded(uid, "convo-a", 3, kind="rejection", company="Acme")
    _threaded(uid, "convo-b", 2, kind="acknowledgement", company="Beta")

    body = client.get("/v1/user/threads", headers=user_headers).json()
    assert body["total"] == 2
    counts = sorted(t["message_count"] for t in body["threads"])
    assert counts == [2, 3], "one row per conversation, carrying its size"


def test_needs_attention_is_the_correctable_pile(client, user_headers):
    """Job-related mail that reached no application. Not 'unread' - we have no
    such concept and inventing one would be a second inbox to maintain."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = _app(uid, company="Matched", title="Engineer")
    done = _threaded(uid, "convo-done", 1, kind="rejection", company="Matched")
    _match(done[0], app_id)
    _threaded(uid, "convo-open", 2, kind="rejection", company="Orphan")

    needs = client.get("/v1/user/threads?needs_attention=true", headers=user_headers).json()
    assert needs["total"] == 1
    assert needs["threads"][0]["message_count"] == 2

    settled = client.get("/v1/user/threads?needs_attention=false", headers=user_headers).json()
    assert settled["total"] == 1, "the filter and its inverse are complements"


def test_a_thread_row_carries_what_a_row_needs(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    ids = _threaded(uid, "convo-rich", 2, kind="offer", company="Acme")
    row = client.get("/v1/user/threads", headers=user_headers).json()["threads"][0]
    assert row["subject"] is not None
    assert row["participants"] == ["a@acme.com"]
    assert "offer" in row["kinds"]
    assert row["latest_message_id"] == ids[-1]
    assert row["last_activity_at"] is not None
