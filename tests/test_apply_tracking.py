from api import db

URL = "https://jobs.ashbyhq.com/ivo-inc/b31e7195-37dd-4631-8648-422cecbb3f83/application?utm_source=Otta"


def open_fill(client, headers):
    result = client.post(
        "/v1/user/apply/resolve",
        headers=headers,
        json={"url": URL, "fields": [{"key": "name", "label": "Name", "kind": "text"}]},
    )
    assert result.status_code == 200, result.text
    return result.json()["fill_id"]


def test_completed_fill_tracks_once_without_claiming_submission(client, user_headers):
    fill_id = open_fill(client, user_headers)
    response = client.post(f"/v1/user/apply/fills/{fill_id}/track", headers=user_headers)
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    assert response.json()["url"] == URL.split("/application")[0]
    repeat = client.post(f"/v1/user/apply/fills/{fill_id}/track", headers=user_headers)
    assert repeat.json()["job_id"] == job_id
    row = db.query_one(
        "SELECT status, date_applied, person_touched_at FROM user_jobs WHERE job_id=%s", (job_id,)
    )
    assert row["status"] is None and row["date_applied"] is None
    assert row["person_touched_at"] is not None
    assert db.query_one("SELECT count(*) AS n FROM tasks WHERE kind='extract_upload'")["n"] == 1
    assert (
        db.query_one("SELECT job_id FROM application_fills WHERE id=%s", (fill_id,))["job_id"]
        == job_id
    )


def test_context_and_repeated_fill_preserve_existing_application(client, user_headers, f):
    job_id = f.make_job(url=URL.split("/application")[0])
    uid = db.query_one("SELECT id FROM users WHERE email='user@example.com'")["id"]
    f.make_board_row(uid, job_id, status="Application Submitted")
    db.execute(
        "UPDATE user_jobs SET date_applied='2026-09-01', notes='keep this' WHERE user_id=%s AND job_id=%s",
        (uid, job_id),
    )
    context = client.get("/v1/user/apply/context", headers=user_headers, params={"url": URL}).json()
    assert context["job"]["status"] == "Application Submitted"
    assert context["job"]["date_applied"] == "2026-09-01"
    fill_id = open_fill(client, user_headers)
    assert (
        client.post(f"/v1/user/apply/fills/{fill_id}/track", headers=user_headers).status_code
        == 200
    )
    row = db.query_one(
        "SELECT status, date_applied::text AS date, notes FROM user_jobs WHERE user_id=%s AND job_id=%s",
        (uid, job_id),
    )
    assert row == {"status": "Application Submitted", "date": "2026-09-01", "notes": "keep this"}
