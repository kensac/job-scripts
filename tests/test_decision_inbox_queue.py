"""One queue holds every decision waiting on a person, and every answer keeps.

These tests cover the questions, writes and measurements whose errors are
silent: overwritten human verdicts, reported but unwritten board updates,
and confirmations that erase the tier they confirmed.
"""

from __future__ import annotations

from api import db
from api.mail import match as mail_match
from tests.decision_inbox_support import _app, _attach, _choices, _items, _msg
from tests.decision_inbox_support import me as me


def test_one_response_carries_all_four_kinds(client, me, f):
    """The point of the endpoint. Four surfaces answered "what is waiting on
    me" separately and their union was something a person had to assemble."""
    headers, uid = me
    job, _url = f.make_ready_job()
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, 'Application Submitted')",
        (uid, job),
    )
    app = _app(uid, job_id=job)
    _attach(_msg(uid, "<all1@x>", "rejection", "Acme"), app)
    _attach(_msg(uid, "<all2@x>", "offer", "Acme"), app)
    _msg(uid, "<all3@x>", "rejection", "Nowhere")
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


def test_a_verb_that_needs_an_argument_says_so(client, me):
    """The last thing a client had to hardcode. Without this it has to know
    that `assign_application` is the verb with a target, which is exactly what
    declaring the verbs exists to avoid - the second such verb would make every
    client wrong."""
    headers, uid = me
    _app(uid)
    _msg(uid, "<needs@x>", "rejection", "Acme")

    choices = _choices(_items(client, headers, "unmatched_message")[0])
    assert choices["assign_application"]["needs_target"] is True
    assert choices["assign_application"]["target_source"] == "candidates"
    # Omission means the verb takes no argument, the same presence-not-value
    # rule `affects` and `reason` follow.
    for verb in ("not_an_application", "not_job_related"):
        assert "needs_target" not in choices[verb]
        assert "target_source" not in choices[verb]


def test_an_ineligible_assign_still_declares_where_its_target_comes_from(client, me):
    """`needs_target` is a property of the VERB, not of this row's luck. A
    client that only learns it from eligible rows learns it from a sample."""
    headers, uid = me
    _msg(uid, "<nocand@x>", "rejection", "Nowhere Inc")

    assign = _choices(_items(client, headers, "unmatched_message")[0])["assign_application"]
    assert assign["eligible"] is False
    assert assign["needs_target"] is True
    assert assign["target_source"] == "candidates"


def test_assign_is_eligible_exactly_when_there_are_candidates(client, me):
    """ON THE QUEUE, where the row's candidates and the eligibility index are
    the same set read under the same key. The frontend expands `candidates`
    when the verb is pressed, so an eligible verb beside an empty list is a
    button nobody can complete.

    The picker does not make this promise and must not be read as if it did:
    its list is search-filtered, and a query matching nothing does not stop an
    application existing.
    """
    headers, uid = me
    _app(uid, company="Acme, Inc.")
    _msg(uid, "<match@x>", "rejection", "Acme")
    _msg(uid, "<nomatch@x>", "rejection", "Nowhere Inc")

    for item in _items(client, headers, "unmatched_message"):
        eligible = _choices(item)["assign_application"]["eligible"]
        assert eligible == bool(item["candidates"]), item["message"]["extracted_company"]
    # And the normalisation is the matcher's, so "Acme" reaches "Acme, Inc.".
    hit = next(i for i in _items(client, headers, "unmatched_message") if i["candidates"])
    assert hit["candidates"][0]["company_name"] == "Acme, Inc."


def test_assigning_to_a_dismissed_application_is_refused(client, me):
    """Declared eligibility has to be enforced where the write happens or it is
    decoration. The server said "no application at this company yet" for a
    dismissed one and then accepted it as a target, so a client that ignored
    `eligible` got its way and mail landed on an application whose whole
    meaning is that it should never have existed."""
    headers, uid = me
    app = _app(uid)
    db.execute("UPDATE applications SET dismissed_at = now() WHERE id = %s", (app,))
    mid = _msg(uid, "<dismissed@x>", "rejection", "Acme")

    assign = _choices(_items(client, headers, "unmatched_message")[0])["assign_application"]
    assert assign["eligible"] is False

    resp = client.post(
        f"/v1/user/resolve/message:{mid}",
        json={"choice": "assign_application", "target": app},
        headers=headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "DISMISSED"
    assert mail_match.latest(mid) is None, "and nothing was written"


def test_each_surface_names_its_own_target_list(client, me):
    """A client reads `payload[choice.target_source]`. The queue calls its list
    `candidates` and the picker calls its list `applications`, so one constant
    would point one of them at a field its own response does not have - the
    hardcoded fact moved one module along rather than removed."""
    headers, uid = me
    _app(uid)
    mid = _msg(uid, "<source@x>", "rejection", "Acme")

    row = _items(client, headers, "unmatched_message")[0]
    source = _choices(row)["assign_application"]["target_source"]
    assert source == "candidates"
    assert row[source], "the queue's declared source resolves against the queue's own payload"

    picker = client.get(f"/v1/user/messages/{mid}/candidates", headers=headers).json()
    picked = {c["choice"]: c for c in picker["choices"]}["assign_application"]
    assert picked["target_source"] == "applications"
    assert picker[picked["target_source"]], "and the picker's against the picker's"
