from __future__ import annotations

from api import db
from tests.conftest import _auth_headers
from tests.decision_inbox_support import _app, _attach, _items, _msg
from tests.decision_inbox_support import me as me


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


def test_rates_answer_the_fleet_wide_question_without_an_owner(client, me, f):
    """/job-scripts is the view across all users, not one user's data behind a
    permission level. "Is `ats_company` right" is the question that decides
    whether a tier keeps attaching mail unattended, and requiring an owner made
    the only useful form of it unaskable."""
    headers, uid = me
    admin = _auth_headers("inbox-admin3", "inboxadmin3@example.com", ["infra-admins"])
    assert client.post("/v1/users/bootstrap", headers=admin).status_code == 200
    _attach(_msg(uid, "<mine@x>", "rejection", "Acme"), _app(uid))

    other = f.make_user()
    _attach(_msg(other, "<theirs@x>", "rejection", "Beta"), _app(other, company="Beta"))

    fleet = client.get("/v1/admin/resolve/rates", headers=admin)
    assert fleet.status_code == 200, fleet.text
    assert fleet.json()["never_reviewed"] == 2, "both users' attachments"

    mine = client.get(f"/v1/admin/resolve/rates?user_id={uid}", headers=admin).json()
    assert mine["never_reviewed"] == 1, "and an owner still narrows it"
    assert headers is not None


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
