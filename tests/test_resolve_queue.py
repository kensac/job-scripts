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


def test_the_candidate_picker_serves_the_same_declared_choices(client, me):
    """The modal is where a person actually makes this decision, and it is
    reached from the mail list as well as the queue. A modal that builds its
    own verb list decides eligibility client-side, which is the thing a
    server-declared contract exists to prevent."""
    headers, uid = me
    mid = _msg(uid, "<pick@x>", "rejection", "Acme")

    body = client.get(f"/v1/user/messages/{mid}/candidates", headers=headers).json()
    choices = {c["choice"]: c for c in body["choices"]}
    assert set(choices) == {"assign_application", "not_an_application", "not_job_related"}
    assert choices["assign_application"]["eligible"] is False
    assert choices["assign_application"]["reason"] == "no application at this company yet"


def test_the_picker_and_the_queue_agree_on_eligibility(client, me):
    """One builder, so the two surfaces cannot drift. If they were built
    separately the first conditional verb would make one of them wrong with
    nothing saying which."""
    headers, uid = me
    db.execute(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, 'Acme', 'tracker')",
        (uid,),
    )
    mid = _msg(uid, "<agree@x>", "rejection", "Acme")

    picker = client.get(f"/v1/user/messages/{mid}/candidates", headers=headers).json()["choices"]
    queued = client.get("/v1/user/resolve/queue", headers=headers).json()["items"][0]["choices"]
    assert picker == queued


def _app(uid: int, company: str) -> int:
    row = db.query_one(
        "INSERT INTO applications (user_id, company_name, source_provenance) "
        "VALUES (%s, %s, 'tracker') RETURNING id",
        (uid, company),
    )
    assert row is not None
    return row["id"]


def _attach(mid: int, app_id: int) -> None:
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s, %s, 'company_name', 'high')",
        (mid, app_id),
    )


def test_a_row_that_would_move_an_application_outranks_one_that_would_not(client, me):
    """A rejection landing on an application still showing "applied" changes
    what the board says. An acknowledgement landing on the same application
    changes nothing, because stage is derived from the strongest event and an
    acknowledgement is never the strongest. Sorting by recency alone put those
    two side by side."""
    headers, uid = me
    app_id = _app(uid, "Acme")
    _attach(_msg(uid, "<seed@x>", "acknowledgement", "Acme"), app_id)
    _msg(uid, "<ack@x>", "acknowledgement", "Acme")
    _msg(uid, "<rej@x>", "rejection", "Acme")

    # Scoped to one kind. The seeded attachment is now a queue row in its own
    # right - a matcher attachment nobody has confirmed - and this test is
    # about how MESSAGES rank against each other.
    items = client.get("/v1/user/resolve/queue?kind=unmatched_message", headers=headers).json()[
        "items"
    ]
    assert items[0]["message"]["classified_as"] == "rejection"
    assert items[0]["rank_reason"] == "answering this moves an application"
    assert items[1]["rank_reason"] == "can be attached, but the stage would not move"


def test_a_message_with_no_application_ranks_last_and_says_why(client, me):
    headers, uid = me
    app_id = _app(uid, "Acme")
    _attach(_msg(uid, "<s2@x>", "acknowledgement", "Acme"), app_id)
    _msg(uid, "<orphan@x>", "rejection", "Nowhere Inc")
    _msg(uid, "<known@x>", "rejection", "Acme")

    items = client.get("/v1/user/resolve/queue", headers=headers).json()["items"]
    assert items[-1]["message"]["extracted_company"] == "Nowhere Inc"
    assert items[-1]["rank_reason"] == "no application at this company yet"


def test_ranking_happens_before_paging(client, me):
    """Sorting inside a page would reorder fifty rows and call it a ranking of
    three and a half thousand: the page looks sensible and the ordering is a
    lie. The high-rank row is oldest here, so a recency sort would bury it."""
    headers, uid = me
    app_id = _app(uid, "Acme")
    _attach(_msg(uid, "<s3@x>", "acknowledgement", "Acme"), app_id)
    old = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, subject, "
        "sent_at, body_text) VALUES (%s, '<old@x>', 'gmail', 'hr@acme.test', 'Old', %s, 'b') "
        "RETURNING id",
        (uid, datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)),
    )
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
        "VALUES (%s, 'rejection', 'high', %s, 'gpt-5-nano')",
        (old["id"], db.jsonb({"company": "Acme"})),
    )
    for i in range(3):
        _msg(uid, f"<new{i}@x>", "acknowledgement", "Acme")

    first = client.get(
        "/v1/user/resolve/queue?limit=1&kind=unmatched_message", headers=headers
    ).json()
    assert first["items"][0]["message"]["id"] == old["id"]
    assert first["total"] == 4, "total counts the whole queue, not the page"


def test_the_queue_says_what_is_below_the_fold(client, me):
    """A page showing fifty of 3,663 must be able to say "40 need you, 3,623
    do not" rather than implying the fifty are all there is."""
    headers, uid = me
    app_id = _app(uid, "Acme")
    _attach(_msg(uid, "<s4@x>", "acknowledgement", "Acme"), app_id)
    _msg(uid, "<r1@x>", "rejection", "Acme")
    _msg(uid, "<o1@x>", "rejection", "Elsewhere Ltd")

    body = client.get(
        "/v1/user/resolve/queue?limit=1&kind=unmatched_message", headers=headers
    ).json()
    assert len(body["items"]) == 1
    # Kind-neutral labels: the queue holds four kinds and three of them are not
    # about attaching anything, so the bucket cannot use the message wording.
    # The narrower sentence is on the row, in `rank_reason`.
    assert body["by_rank"]["answering this changes what the product says"] == 1
    assert body["by_rank"]["only a refusal is available"] == 1


def test_nothing_is_hidden_from_the_queue(client, me):
    """Every row is still returned. Nothing in this population is
    unresolvable - a person can refuse any of it - so "low priority" is the
    honest claim and "cannot be settled" is not."""
    headers, uid = me
    for i in range(4):
        _msg(uid, f"<keep{i}@x>", "acknowledgement", "Nowhere Inc")

    body = client.get("/v1/user/resolve/queue", headers=headers).json()
    assert body["total"] == 4
    assert len(body["items"]) == 4
