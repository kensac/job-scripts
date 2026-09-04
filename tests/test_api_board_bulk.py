"""A selection on the board is one request, not one request per row."""

from __future__ import annotations

from api import db
from tests.factories import make_job


def _user_id() -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert row is not None
    return row["id"]


def _rows(uid: int) -> dict[int, dict]:
    return {
        r["job_id"]: r
        for r in db.query(
            "SELECT job_id, status, date_applied, notes FROM user_jobs WHERE user_id = %s", (uid,)
        )
    }


def test_bulk_patch_applies_one_patch_to_every_selected_row(client, user_headers):
    uid = _user_id()
    ids = [make_job(url=f"https://x.test/{i}") for i in range(3)]
    r = client.patch(
        "/v1/user/jobs",
        json={"job_ids": ids, "patch": {"status": "applied", "notes": "batch"}},
        headers=user_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 3
    rows = _rows(uid)
    assert all(rows[i]["status"] == "applied" and rows[i]["notes"] == "batch" for i in ids)
    # Setting a status stamps date_applied once, as the single-row write does.
    assert all(rows[i]["date_applied"] is not None for i in ids)
    history = db.query(
        "SELECT job_id, new_status FROM user_job_history WHERE user_id = %s ORDER BY job_id",
        (uid,),
    )
    assert [(h["job_id"], h["new_status"]) for h in history] == [(i, "applied") for i in ids]


def test_bulk_patch_skips_what_the_caller_may_not_touch_and_refuses_an_empty_patch(
    client, user_headers, other_user_headers
):
    uid = _user_id()
    mine = make_job(url="https://x.test/mine")
    other = db.query_one("SELECT id FROM users WHERE sub != 'test-user' ORDER BY id LIMIT 1")
    assert other is not None
    private = make_job(url="https://x.test/private", uploaded_by=other["id"])
    r = client.patch(
        "/v1/user/jobs",
        json={"job_ids": [mine, private, 999999], "patch": {"status": "applied"}},
        headers=user_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1 and r.json()["skipped"] == [private, 999999]
    assert set(_rows(uid)) == {mine}

    r = client.patch("/v1/user/jobs", json={"job_ids": [mine], "patch": {}}, headers=user_headers)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "EMPTY_PATCH"


def test_bulk_delete_drops_only_the_callers_rows(client, user_headers, other_user_headers):
    uid = _user_id()
    ids = [make_job(url=f"https://x.test/d{i}") for i in range(2)]
    for i in ids:
        client.patch(f"/v1/user/jobs/{i}", json={"notes": "keep"}, headers=user_headers)
        client.patch(f"/v1/user/jobs/{i}", json={"notes": "theirs"}, headers=other_user_headers)
    r = client.request(
        "DELETE", "/v1/user/jobs", json={"job_ids": [*ids, 999999]}, headers=user_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 2
    assert _rows(uid) == {}
    assert db.query_one("SELECT count(*) AS n FROM user_jobs")["n"] == 2
