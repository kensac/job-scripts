from __future__ import annotations

from api import db
from api.mail import match as mail_match
from tasks.mail.matching import handler as match_task
from tests.decision_inbox_support import _app, _attach, _choices, _items, _msg
from tests.decision_inbox_support import me as me


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
    assert (current.method, current.application_id) == (mail_match.DETACHED, None)


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
    mail_match.record(
        mid, mail_match.MatchDecision(app, "ats_company", "medium", "single application")
    )

    current = mail_match.latest(mid)
    assert current is not None
    assert current.application_id is None, "the person's answer still stands"
    assert current.actor_user_id == uid


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
    assert current.method == mail_match.NOT_AN_APPLICATION
    assert current.actor_user_id == uid
    assert app is not None


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
            f"/v1/user/pipeline/{app}/matches/{match.id}/detach", json={}, headers=headers
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
        f"/v1/user/resolve/match:{match.id}", json={"choice": "reject_match"}, headers=headers
    )

    rows = db.query(
        "SELECT method, actor_user_id FROM application_matches WHERE message_id = %s ORDER BY id",
        (mid,),
    )
    assert [r["method"] for r in rows] == ["ats_company", "ats_company", mail_match.DETACHED]
    assert [r["actor_user_id"] for r in rows] == [None, uid, uid]


def test_a_detach_from_the_pipeline_records_who_did_it(client, me):
    """Record the person who detached this match.

    A 2026-10-11 audit found 37 manual rows without an actor after three
    endpoints wrote directly. Without the actor, a reader cannot tell whether
    someone reviewed the match.
    """
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
    assert current.actor_user_id == uid
