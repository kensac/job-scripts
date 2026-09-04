"""One queue holds every decision waiting on a person, and every answer keeps.

The four populations here were all produced and none of them could be
answered: 4,674 attachments nobody was asked about, 1,159 status proposals
against 0 answers, 525 open action items, and 3,251 unmatched messages. These
tests constrain the parts where being wrong is silent - a human verdict the
matcher overwrites, a board update that is reported and not written, a
confirmation that erases the tier it confirmed.
"""

from __future__ import annotations

import datetime

import pytest

from api import db, mail_match
from api.tasks import mail_match as match_task
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
    db.execute(
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
    row = db.query_one(
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


def test_an_attachment_nobody_asked_about_is_a_question(client, me):
    """The matcher placed it and the queue's own filter then hid it, so a
    medium-confidence guess and a confirmed fact looked identical."""
    headers, uid = me
    app = _app(uid)
    mid = _msg(uid, "<unconf@x>", "rejection", "Acme")
    _attach(mid, app)

    items = _items(client, headers, "unconfirmed_match")
    assert len(items) == 1
    assert items[0]["match"]["method"] == "ats_company"
    assert set(_choices(items[0])) == {"confirm_match", "reject_match"}
    # A rejection is what holds this application at `rejected`, so removing it
    # would move the stage - which is what makes it worth asking about first.
    assert items[0]["rank"] == 3
    assert items[0]["application"]["stage"] == "rejected"


def test_confirming_keeps_the_tier_and_records_the_person(client, me):
    """Restamping a confirmation as `manual` would destroy the only thing
    worth measuring: whether ats_company at medium is right often enough."""
    headers, uid = me
    app = _app(uid)
    mid = _msg(uid, "<conf@x>", "rejection", "Acme")
    first = _attach(mid, app)

    item = _items(client, headers, "unconfirmed_match")[0]
    resp = client.post(
        f"/v1/user/resolve/{item['id']}", json={"choice": "confirm_match"}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    rows = db.query(
        "SELECT id, application_id, method, confidence, actor_user_id FROM application_matches "
        "WHERE message_id = %s ORDER BY id",
        (mid,),
    )
    assert len(rows) == 2, "an append, not an edit"
    assert rows[0]["id"] == first and rows[0]["actor_user_id"] is None, "the unreviewed row keeps"
    assert rows[1]["actor_user_id"] == uid
    assert rows[1]["application_id"] == app, "confirming does not move the attachment"
    assert (rows[1]["method"], rows[1]["confidence"]) == ("ats_company", "medium")
    assert _items(client, headers, "unconfirmed_match") == [], "and it stops being a question"


def test_rejecting_returns_the_message_to_the_queue_and_deletes_nothing(client, me):
    headers, uid = me
    app = _app(uid)
    mid = _msg(uid, "<rej@x>", "rejection", "Acme")
    _attach(mid, app)

    item = _items(client, headers, "unconfirmed_match")[0]
    resp = client.post(
        f"/v1/user/resolve/{item['id']}", json={"choice": "reject_match"}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    unmatched = _items(client, headers, "unmatched_message")
    assert [i["message"]["id"] for i in unmatched] == [mid]
    assert len(db.query("SELECT id FROM application_matches WHERE message_id = %s", (mid,))) == 2
    current = mail_match.latest(mid)
    assert current is not None
    assert (current["method"], current["application_id"]) == (mail_match.DETACHED, None)


def test_the_matcher_does_not_overturn_a_person(client, me):
    """This is not hypothetical. The single human decision ever recorded in
    production - match 14911, message 126111 - was superseded an hour later by
    the matcher writing `unmatched`, because the sweep had selected the message
    before the person answered and wrote its verdict afterwards. A selection
    predicate cannot close that window; the check has to be at the write."""
    headers, uid = me
    app = _app(uid)
    mid = _msg(uid, "<race@x>", "rejection", "Acme")
    _attach(mid, app)
    item = _items(client, headers, "unconfirmed_match")[0]
    assert (
        client.post(
            f"/v1/user/resolve/{item['id']}", json={"choice": "reject_match"}, headers=headers
        ).status_code
        == 200
    )

    # The verdict a sweep computed against the world as it was before the
    # rejection, arriving after it.
    mail_match.record(mid, mail_match.Match(app, "ats_company", "medium", "single application"))

    current = mail_match.latest(mid)
    assert current is not None
    assert current["application_id"] is None, "the person's answer still stands"
    assert current["actor_user_id"] == uid


def test_the_sweep_leaves_a_human_decided_message_alone(client, me):
    """The same rule reached through the real handler rather than through
    `record` directly, because match_pending is where the cost of re-deciding
    is paid and its own predicate does not exclude these."""
    headers, uid = me
    app = _app(uid)
    mid = _msg(uid, "<sweep@x>", "rejection", "Acme")
    assert (
        client.post(
            f"/v1/user/resolve/message:{mid}",
            json={"choice": "not_an_application"},
            headers=headers,
        ).status_code
        == 200
    )

    _app(uid, company="Acme")  # a new application: what makes the sweep re-look
    match_task.match_pending(uid)

    current = mail_match.latest(mid)
    assert current is not None
    assert current["method"] == mail_match.NOT_AN_APPLICATION
    assert current["actor_user_id"] == uid
    assert app is not None


def test_a_proposal_reaches_an_application_with_no_board_row(client, me):
    """`JOIN user_jobs` asked what the board said of applications that have no
    board row, which is 1,817 of 2,543. It hid 947 proposals by never forming
    the question."""
    headers, uid = me
    app = _app(uid, job_id=None)
    mid = _msg(uid, "<prop@x>", "rejection", "Acme")
    _attach(mid, app)

    offered = client.get("/v1/user/suggestions", headers=headers).json()
    assert offered["total"] == 1
    assert offered["suggestions"][0]["suggested_status"] == "Rejected"
    assert offered["suggestions"][0]["board_updatable"] is False
    assert offered["suggestions"][0]["evidence"] is not None


def test_accepting_a_proposal_reports_what_it_actually_touched(client, me):
    """It used to return the proposed status whenever the answer was accepted,
    including where the UPDATE matched nothing - so the caller was told a
    status had moved that no SELECT could find."""
    headers, uid = me
    app = _app(uid, job_id=None)
    mid = _msg(uid, "<noboard@x>", "rejection", "Acme")
    _attach(mid, app)

    item = _items(client, headers, "status_proposal")[0]
    assert item["implies"]["board_updated"] is False
    assert item["implies"]["board_status"] == "Rejected"

    resp = client.post(
        f"/v1/user/resolve/{item['id']}", json={"choice": "accept_status"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["board_updated"] is False
    assert "board_status" not in body, "it must not name a status it did not write"
    assert "not on your board" in body["reason"]
    # Recorded either way: a proposal that cannot move the board is still an
    # answer, and it is the only evidence the mapping was right.
    assert (
        db.query_one("SELECT response FROM suggestion_responses WHERE application_id = %s", (app,))[
            "response"
        ]
        == "accepted"
    )


def test_accepting_moves_a_board_that_exists(client, me, f):
    headers, uid = me
    job, _url = f.make_ready_job()
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status, date_applied) "
        "VALUES (%s, %s, 'Application Submitted', %s)",
        (uid, job, SENT.date()),
    )
    app = _app(uid, job_id=job)
    mid = _msg(uid, "<board@x>", "rejection", "Acme")
    _attach(mid, app)

    item = _items(client, headers, "status_proposal")[0]
    assert item["implies"] == {
        "board_status": "Rejected",
        "from_status": "Application Submitted",
        "board_updated": True,
    }
    body = client.post(
        f"/v1/user/resolve/{item['id']}", json={"choice": "accept_status"}, headers=headers
    ).json()
    assert body["board_updated"] is True and body["board_status"] == "Rejected"
    assert (
        db.query_one("SELECT status FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, job))[
            "status"
        ]
        == "Rejected"
    )


def test_an_action_says_what_would_close_it_without_a_person(client, me):
    """`settles_on` is what tells "waiting on the next email" from "waiting on
    you", and it is a property of the kind rather than of the item's age. An
    approach you never answered has an empty one by construction; an assessment
    invite is closed by the acknowledgement that follows it."""
    from api import mail_pipeline

    headers, uid = me
    app = _app(uid)
    _attach(_msg(uid, "<assess@x>", "assessment_invite", "Acme"), app)
    outreach = _msg(uid, "<recruit@x>", "recruiter_outreach", "Acme")
    _attach(outreach, app)
    mail_pipeline.sync_action_items(app)

    by_kind = {i["action"]["kind"]: i for i in _items(client, headers, "action_item")}
    assert by_kind["reply_to_recruiter"]["action"]["settles_on"] == []
    assert "only you can" in by_kind["reply_to_recruiter"]["rank_reason"]
    assert "acknowledgement" in by_kind["complete_assessment"]["action"]["settles_on"]
    assert "would close this" in by_kind["complete_assessment"]["rank_reason"]
    # One rank for both. Marking either done closes the ask and moves no stage,
    # so the question the queue orders by has the same answer for each.
    assert {i["rank"] for i in by_kind.values()} == {2}


def test_one_response_carries_all_four_kinds(client, me, f):
    """The point of the endpoint. Four surfaces answered "what is waiting on
    me" separately and their union was something a person had to assemble."""
    headers, uid = me
    job, _url = f.make_ready_job()
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status, date_applied) "
        "VALUES (%s, %s, 'Application Submitted', %s)",
        (uid, job, SENT.date()),
    )
    app = _app(uid, job_id=job)
    _attach(_msg(uid, "<all1@x>", "rejection", "Acme"), app)
    _attach(_msg(uid, "<all2@x>", "offer", "Acme"), app)
    _msg(uid, "<all3@x>", "rejection", "Nowhere")
    from api import mail_pipeline

    mail_pipeline.sync_action_items(app)

    body = client.get("/v1/user/resolve/queue?limit=200", headers=headers).json()
    assert set(body["by_kind"]) == {
        "unmatched_message",
        "unconfirmed_match",
        "status_proposal",
        "action_item",
    }
    assert sum(body["by_kind"].values()) == body["total"] == len(body["items"])
    # Every kind reachable on its own, and the parts summing to the whole.
    per_kind = sum(
        client.get(f"/v1/user/resolve/queue?kind={k}", headers=headers).json()["total"]
        for k in body["by_kind"]
    )
    assert per_kind == body["total"]


def test_an_unknown_kind_is_refused_rather_than_ignored(client, me):
    headers, _uid = me
    resp = client.get("/v1/user/resolve/queue?kind=nonsense", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "UNKNOWN_KIND"


def test_a_verb_from_another_kind_is_refused(client, me):
    headers, uid = me
    mid = _msg(uid, "<wrong@x>", "rejection", "Acme")
    resp = client.post(
        f"/v1/user/resolve/message:{mid}", json={"choice": "confirm_match"}, headers=headers
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "WRONG_CHOICE"


def test_answering_a_superseded_attachment_is_refused(client, me):
    """A queue page can be minutes old. Confirming an attachment that has since
    been replaced would write back a decision about a world that is gone."""
    headers, uid = me
    app = _app(uid)
    mid = _msg(uid, "<stale@x>", "rejection", "Acme")
    old = _attach(mid, app)
    _attach(mid, app, method="derived")

    resp = client.post(
        f"/v1/user/resolve/match:{old}", json={"choice": "confirm_match"}, headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "STALE"


def test_one_user_cannot_answer_anothers_attachment(client, me, f):
    headers, _uid = me
    theirs = f.make_user()
    app = _app(theirs)
    mid = _msg(theirs, "<theirs@x>", "rejection", "Acme")
    match = _attach(mid, app)

    resp = client.post(
        f"/v1/user/resolve/match:{match}", json={"choice": "confirm_match"}, headers=headers
    )
    assert resp.status_code == 404


def test_history_keeps_the_answer_that_was_overturned(client, me):
    """A decision that vanishes when it is reversed takes the evidence that the
    rule was wrong with it."""
    headers, uid = me
    app = _app(uid)
    mid = _msg(uid, "<hist@x>", "rejection", "Acme")
    _attach(mid, app)

    item = _items(client, headers, "unconfirmed_match")[0]
    assert (
        client.post(
            f"/v1/user/resolve/{item['id']}", json={"choice": "confirm_match"}, headers=headers
        ).status_code
        == 200
    )
    later = _items(client, headers, "unconfirmed_match")
    assert later == [], "confirming settles it"
    # Overturn it from the application side, which is the other door onto the
    # same fact and must land in the same log.
    match = mail_match.latest(mid)
    assert match is not None
    assert (
        client.post(
            f"/v1/user/pipeline/{app}/matches/{match['id']}/detach", json={}, headers=headers
        ).status_code
        == 200
    )

    history = client.get("/v1/user/resolve/history", headers=headers).json()
    assert history["total"] == 2, "both answers, not just the surviving one"
    newest, older = history["decisions"]
    assert newest["decision"] == "rejected"
    assert "superseded_by" not in newest, "nothing has overturned the newest answer"
    assert older["decision"] == "attached" and older["superseded_by"] == newest["id"]
    assert newest["supersedes"] == older["id"]
    assert {d["by"] for d in history["decisions"]} == {"you"}


def test_confirm_then_reject_leaves_three_rows(client, me):
    headers, uid = me
    app = _app(uid)
    mid = _msg(uid, "<three@x>", "rejection", "Acme")
    _attach(mid, app)

    confirm = _items(client, headers, "unconfirmed_match")[0]
    client.post(
        f"/v1/user/resolve/{confirm['id']}", json={"choice": "confirm_match"}, headers=headers
    )
    match = mail_match.latest(mid)
    assert match is not None
    client.post(
        f"/v1/user/resolve/match:{match['id']}", json={"choice": "reject_match"}, headers=headers
    )

    rows = db.query(
        "SELECT method, actor_user_id FROM application_matches WHERE message_id = %s ORDER BY id",
        (mid,),
    )
    assert [r["method"] for r in rows] == ["ats_company", "ats_company", mail_match.DETACHED]
    assert [r["actor_user_id"] for r in rows] == [None, uid, uid]


def test_a_detach_from_the_pipeline_records_who_did_it(client, me):
    """Three endpoints wrote this table with a raw INSERT and none set the
    actor, which is why 37 `manual` rows in production carry none and why
    "has a person looked at this" was unanswerable by query."""
    headers, uid = me
    app = _app(uid)
    mid = _msg(uid, "<pipe@x>", "rejection", "Acme")
    match = _attach(mid, app)

    assert (
        client.post(
            f"/v1/user/pipeline/{app}/matches/{match}/detach", json={}, headers=headers
        ).status_code
        == 200
    )
    current = mail_match.latest(mid)
    assert current is not None
    assert current["actor_user_id"] == uid


def test_never_reviewed_is_one_query_and_a_review_moves_it(client, me):
    """The count the whole feature is measured by, asked the way any reader
    would ask it: the current row per message, holding an application, with no
    actor on it."""
    headers, uid = me
    app = _app(uid)
    for i in range(3):
        _attach(_msg(uid, f"<nr{i}@x>", "rejection", "Acme"), app)

    sql = """
        WITH current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id, actor_user_id
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT count(*) AS c FROM current_match
        WHERE application_id IS NOT NULL AND actor_user_id IS NULL
    """
    assert db.query_one(sql)["c"] == 3
    item = _items(client, headers, "unconfirmed_match")[0]
    client.post(f"/v1/user/resolve/{item['id']}", json={"choice": "confirm_match"}, headers=headers)
    assert db.query_one(sql)["c"] == 2


def test_rates_say_not_measured_rather_than_zero(client, me):
    """A tier nobody has reviewed has no confirm rate. Rendering that as 0%
    says the tier is always wrong, which is the opposite of what it means."""
    headers, uid = me
    admin = _auth_headers("inbox-admin", "inboxadmin@example.com", ["infra-admins"])
    assert client.post("/v1/users/bootstrap", headers=admin).status_code == 200
    app = _app(uid)
    _attach(_msg(uid, "<r1@x>", "rejection", "Acme"), app)
    _attach(_msg(uid, "<r2@x>", "rejection", "Acme"), app, method="derived")

    body = client.get(f"/v1/admin/resolve/rates?user_id={uid}", headers=admin).json()
    assert body["never_reviewed"] == 2 and body["reviewed"] == 0
    for row in body["by_method"]:
        assert "confirm_rate" not in row
        assert row["note"].startswith("not measured")

    item = next(
        i
        for i in _items(client, headers, "unconfirmed_match")
        if i["match"]["method"] == "ats_company"
    )
    client.post(f"/v1/user/resolve/{item['id']}", json={"choice": "confirm_match"}, headers=headers)

    body = client.get(f"/v1/admin/resolve/rates?user_id={uid}", headers=admin).json()
    rates = {r["method"]: r for r in body["by_method"]}
    assert rates["ats_company"]["confirmed"] == 1
    assert rates["ats_company"]["confirm_rate"] == 1.0
    # The tier the person did NOT look at keeps saying so rather than
    # inheriting the other one's rate.
    assert "confirm_rate" not in rates["derived"]
    assert body["never_reviewed"] == 1


def test_a_rejection_is_counted_against_the_tier_that_made_the_attachment(client, me):
    """A `detached` row names no tier, so counting a rejection by its own
    method would put every one of them in one bucket and no tier would ever
    look wrong."""
    headers, uid = me
    admin = _auth_headers("inbox-admin2", "inboxadmin2@example.com", ["infra-admins"])
    assert client.post("/v1/users/bootstrap", headers=admin).status_code == 200
    app = _app(uid)
    _attach(_msg(uid, "<rr@x>", "rejection", "Acme"), app, method="company_title")

    item = _items(client, headers, "unconfirmed_match")[0]
    client.post(f"/v1/user/resolve/{item['id']}", json={"choice": "reject_match"}, headers=headers)

    rates = {
        r["method"]: r
        for r in client.get(f"/v1/admin/resolve/rates?user_id={uid}", headers=admin).json()[
            "by_method"
        ]
    }
    assert rates["company_title"]["rejected"] == 1
    assert rates["company_title"]["confirm_rate"] == 0.0
    assert "detached" not in rates
