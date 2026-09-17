"""Read surfaces retain scope and never turn missing evidence into free work."""

from api import db
from tests.test_api_jobs import _insert_job, _uid


def decision(
    *,
    task=1,
    user=None,
    url="https://example.test/a",
    action="review",
    mode="off",
    stage="detailed",
    prompt="p",
    model="m",
    age=0,
):
    return db.query_one(
        "INSERT INTO review_gate_decisions(task_id,url,user_id,prompt_hash,stage,mode,action,title,policy,evidence,created_at) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,'Engineer','{}',%s,now()-(%s * interval '1 day')) RETURNING id",
        (
            task,
            url,
            user,
            prompt,
            stage,
            mode,
            action,
            db.jsonb({"planned_model": model, "transport": "batch"}),
            age,
        ),
    )["id"]


def outcome(did, qid, cost, rejected=False):
    db.execute(
        "INSERT INTO review_gate_outcomes(decision_id,query_id,model,rejected,outcome,recorded_cost_usd,usage) "
        "VALUES(%s,%s,'m',%s,'written',%s,'{}')",
        (did, qid, rejected, cost),
    )


def test_gate_report_separates_paid_outcomes_decisions_and_estimates(client, admin_headers):
    did = decision()
    outcome(did, 1, 0.2)
    outcome(did, 2, 0.4)
    decision(task=2, action="skip", mode="enforce", stage="title")
    decision(task=3, action="skip", mode="enforce", stage="title", model="different")
    decision(task=4, age=10)
    response = client.get("/v1/admin/review-gates/report?days=7", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert sum(row["decisions"] for row in body["rows"]) == 3
    paid = next(row for row in body["rows"] if row["action"] == "review")
    assert paid["recorded_outcomes"] == 2
    assert abs(paid["actual_cost_usd"] - 0.6) < 0.00001
    estimate = body["avoided_cost"]
    assert abs(estimate["estimated_avoided_cost_usd"] - 0.6) < 0.00001
    assert estimate["estimated_decisions"] == 1
    assert estimate["unestimated_decisions"] == 1
    assert estimate["reference_outcomes"] == 2


def test_gate_report_unknown_cost_is_not_zero_or_a_savings_baseline(client, admin_headers):
    did = decision()
    outcome(did, 1, None)
    outcome(did, 2, 0.2)
    decision(task=2, action="skip", mode="enforce", stage="profile")
    body = client.get("/v1/admin/review-gates/report", headers=admin_headers).json()
    paid = next(row for row in body["rows"] if row["action"] == "review")
    assert paid["actual_cost_usd"] is None
    assert paid["known_cost_usd"] == 0.2
    assert paid["unpriced_outcomes"] == 1
    assert body["avoided_cost"]["estimated_avoided_cost_usd"] is None
    assert body["avoided_cost"]["unestimated_decisions"] == 1


def test_decisions_are_paginated_and_personal_scope_cannot_leak(
    client, admin_headers, user_headers, other_user_headers
):
    me, other = _uid(user_headers), _uid(other_user_headers)
    url = "https://example.test/owned"
    jid = _insert_job("manual", url, uploaded_by=me)
    mine = decision(user=me, url=url)
    decision(task=2, user=other, url=url)
    body = client.get(f"/v1/user/jobs/{jid}/review-decisions", headers=user_headers).json()
    assert body["total"] == 1
    assert body["rows"][0]["id"] == mine
    assert body["rows"][0]["policy"] == {}
    assert body["rows"][0]["evidence"] == {}
    assert (
        client.get(f"/v1/user/jobs/{jid}/review-decisions", headers=other_user_headers).status_code
        == 404
    )
    assert client.get("/v1/admin/review-gates/decisions", headers=user_headers).status_code == 403
    admin = client.get("/v1/admin/review-gates/decisions?page_size=1", headers=admin_headers).json()
    assert admin["total"] == 2 and admin["has_more"]
    assert len(admin["rows"]) == 1
    scoped = client.get(f"/v1/admin/review-gates/report?user={me}", headers=admin_headers).json()
    assert sum(row["decisions"] for row in scoped["rows"]) == 1
    assert scoped["filters"] == {"user": [str(me)]}


def test_timeline_exposes_gate_only_history_and_missing_outcomes(client, admin_headers):
    decision(action="skip", mode="enforce", stage="title")
    decision(task=2, url="https://example.test/no-receipt")
    body = client.get(
        "/v1/admin/jobs/timeline?url=https://example.test/a", headers=admin_headers
    ).json()
    assert body["rows"] == []
    assert body["decisions"]["total"] == 1
    report = client.get("/v1/admin/review-gates/report", headers=admin_headers).json()
    row = next(row for row in report["rows"] if row["action"] == "review")
    assert row["without_recorded_outcome"] == 1
    assert row["actual_cost_usd"] is None
