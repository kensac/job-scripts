"""An admin closes a posting for everyone: a closed verdict row, the same
thing the closed check writes, so it leaves every board on the next read and
the report about it says so."""

from __future__ import annotations

from tests.test_api_jobs import _insert_job, _job_ids, _pass_closed, _subscribe, _uid


def test_closing_a_reported_posting_removes_it_from_the_board(client, user_headers, admin_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-cl", "https://x.test/cl1", company="Pools R Us", title="Engineer")
    _subscribe(uid, "src-cl")
    _pass_closed("https://x.test/cl1")
    assert jid in _job_ids(client.get("/v1/user/jobs", headers=user_headers).json())
    r = client.post(
        f"/v1/user/jobs/{jid}/report",
        json={"kind": "other", "message": "pool rental"},
        headers=user_headers,
    )
    assert r.status_code == 200, r.text

    reports = client.get("/v1/admin/reports", headers=admin_headers).json()
    assert reports["can_close_posting"] is True
    row = next(x for x in reports["rows"] if x["job_id"] == jid)
    assert row["posting_closed"] is False

    r = client.post(
        f"/v1/admin/jobs/{jid}/close", json={"reason": "not a job"}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["posting_closed"] is True and "not a job" in r.json()["reason"]
    assert jid not in _job_ids(client.get("/v1/user/jobs", headers=user_headers).json())
    row = next(
        x
        for x in client.get("/v1/admin/reports", headers=admin_headers).json()["rows"]
        if x["job_id"] == jid
    )
    assert row["posting_closed"] is True
    assert (
        client.post("/v1/admin/jobs/999999/close", json={}, headers=admin_headers).status_code
        == 404
    )
