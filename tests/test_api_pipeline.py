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
