from __future__ import annotations

from api import db
from tests.decision_inbox_support import _app, _attach, _items, _msg
from tests.decision_inbox_support import me as me


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
    # A whole sentence, because the client renders it verbatim rather than
    # composing its own line about what happened.
    assert body["reason"] == "Recorded. This application is not on your board, so no status moved."
    # Recorded either way: a proposal that cannot move the board is still an
    # answer, and it is the only evidence the mapping was right.
    assert (
        db.query_one(
            "SELECT answer FROM event_answers WHERE question = 'status' AND event_id IN "
            "(SELECT e.id FROM email_events e JOIN application_matches am "
            "ON am.message_id = e.message_id WHERE am.application_id = %s)",
            (app,),
        )["answer"]
        == "accepted"
    )


def test_accepting_moves_a_board_that_exists(client, me, f):
    headers, uid = me
    job, _url = f.make_ready_job()
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, 'Application Submitted')",
        (uid, job),
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


def test_a_proposal_carries_what_a_person_needs_to_check_it(client, me, f):
    """A proposal row shipped with a bare message id and a null stage. The
    person was shown a company, a proposed status, and nothing to check either
    against: not the subject, not the sender, not what the application's other
    mail had already put it at, not the posting it would move."""
    headers, uid = me
    job, _url = f.make_ready_job()
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, 'Application Submitted')",
        (uid, job),
    )
    app = _app(uid, job_id=job)
    _attach(_msg(uid, "<ack@x>", "acknowledgement", "Acme"), app)
    _attach(_msg(uid, "<rej@x>", "rejection", "Acme"), app)

    item = _items(client, headers, "status_proposal")[0]
    # The same message block every other kind carries, so one card renders
    # all four rather than a thinner one for this kind.
    assert item["message"]["subject"] == "Update"
    assert item["message"]["from_email"] == "hr@acme.test"
    assert item["message"]["classified_as"] == "rejection"
    assert item["message"]["extracted_company"] == "Acme"
    # The stage is what the board derives from ALL of this application's mail,
    # read from the same function, so the queue cannot disagree with it.
    assert item["application"]["stage"] == "rejected"
    assert item["application"]["job_id"] == job

    # No posting is an omitted key, not a null: presence is how the client
    # decides whether there is a posting to open.
    orphan = _app(uid, company="Nowhere", job_id=None)
    _attach(_msg(uid, "<orphan@x>", "rejection", "Nowhere"), orphan)
    by_app = {i["application"]["id"]: i for i in _items(client, headers, "status_proposal")}
    assert "job_id" not in by_app[orphan]["application"]
    assert by_app[orphan]["application"]["stage"] == "rejected"


def test_an_action_says_what_would_close_it_without_a_person(client, me):
    """`settles_on` is what tells "waiting on the next email" from "waiting on
    you", and it is a property of the kind rather than of the item's age. An
    offer is closed by nothing but a rejection; an assessment invite is closed
    by the acknowledgement that follows it. A recruiter approach asks nothing:
    it is attached to no application."""
    headers, uid = me
    app = _app(uid)
    _attach(_msg(uid, "<assess@x>", "assessment_invite", "Acme"), app)
    _attach(_msg(uid, "<offer@x>", "offer", "Acme"), app)
    _attach(_msg(uid, "<recruit@x>", "recruiter_outreach", "Acme"), app)

    by_kind = {i["action"]["kind"]: i for i in _items(client, headers, "action_item")}
    assert set(by_kind) == {"complete_assessment", "respond_to_offer"}
    assert by_kind["respond_to_offer"]["action"]["settles_on"] == ["rejection"]
    assert "acknowledgement" in by_kind["complete_assessment"]["action"]["settles_on"]
    assert "would close this" in by_kind["complete_assessment"]["rank_reason"]
    # One rank for both. Marking either done closes the ask and moves no stage,
    # so the question the queue orders by has the same answer for each.
    assert {i["rank"] for i in by_kind.values()} == {2}
