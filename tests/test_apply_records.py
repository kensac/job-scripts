"""Apply reports, resume limits, submission receipts, and board status."""

from __future__ import annotations

import base64
import json
import uuid

from api import db
from tests.job_board_support import _insert_job, _uid


def test_a_report_keeps_the_page_whole_for_triage(client, user_headers, other_user_headers):
    page = {"title": "Apply", "fields": [{"key": "k", "label": "Odd"}], "html": "<form/>"}
    made = client.post(
        "/v1/user/apply/reports",
        json={"url": "https://x.test/apply", "note": "the select did not take", "page": page},
        headers=user_headers,
    )
    assert made.status_code == 201, made.text
    listed = client.get("/v1/user/apply/reports", headers=user_headers).json()["reports"]
    assert listed[0]["title"] == "Apply" and listed[0]["fields"] == 1
    assert listed[0]["note"] == "the select did not take"
    full = client.get(f"/v1/user/apply/reports/{made.json()['id']}", headers=user_headers).json()
    assert full["page"] == page
    assert client.get("/v1/user/apply/reports", headers=other_user_headers).json()["reports"] == []
    too_big = {"html": "x" * 2_100_000}
    assert (
        client.post(
            "/v1/user/apply/reports",
            json={"url": "https://x.test/apply", "page": too_big},
            headers=user_headers,
        ).status_code
        == 413
    )


def test_chunked_report_retries_and_retains_complete_page(client, user_headers, other_user_headers):
    upload_id = str(uuid.uuid4())
    page = {"title": "Large form", "fields": [{"key": "large"}], "html": "a" * 2_100_000 + "🧭"}
    data = json.dumps(page, ensure_ascii=False).encode("utf-8")
    chunks = [data[i : i + 65_536] for i in range(0, len(data), 65_536)]
    path = f"/v1/user/apply/report-uploads/{upload_id}"
    start = {
        "id": upload_id,
        "url": "https://x.test/apply",
        "note": "large form",
        "title": page["title"],
        "field_count": 1,
        "chunk_count": len(chunks),
    }
    created = client.post("/v1/user/apply/report-uploads", json=start, headers=user_headers)
    assert created.status_code == 201, created.text
    assert client.post(f"{path}/complete", headers=user_headers).status_code == 409
    assert (
        client.put(f"{path}/chunks/0", content=chunks[0], headers=other_user_headers).status_code
        == 404
    )
    for index in reversed(range(len(chunks))):
        response = client.put(f"{path}/chunks/{index}", content=chunks[index], headers=user_headers)
        assert response.status_code == 200, response.text
    assert (
        client.put(f"{path}/chunks/0", content=chunks[0], headers=user_headers).status_code == 200
    )
    assert (
        client.put(f"{path}/chunks/0", content=b"changed", headers=user_headers).status_code == 409
    )
    report = client.post(f"{path}/complete", headers=user_headers)
    assert report.status_code == 201, report.text
    assert client.post(f"{path}/complete", headers=user_headers).json() == report.json()
    assert (
        client.put(f"{path}/chunks/0", content=chunks[0], headers=user_headers).status_code == 200
    )
    assert (
        client.put(f"{path}/chunks/0", content=b"changed", headers=user_headers).status_code == 409
    )
    report_id = report.json()["id"]
    detail = client.get(f"/v1/user/apply/reports/{report_id}", headers=user_headers).json()
    assert detail["page"] is None and detail["chunk_count"] == len(chunks)
    assert (
        client.get(f"/v1/user/apply/reports/{report_id}", headers=other_user_headers).status_code
        == 404
    )
    stored = b"".join(
        base64.b64decode(
            client.get(
                f"/v1/user/apply/reports/{report_id}/chunks/{index}", headers=user_headers
            ).json()["data"]
        )
        for index in range(len(chunks))
    )
    assert stored == data
    assert json.loads(stored) == page
    listed = client.get("/v1/user/apply/reports", headers=user_headers).json()["reports"]
    assert listed[0]["title"] == "Large form" and listed[0]["fields"] == 1


def test_chunked_report_refuses_oversized_part_without_losing_upload(client, user_headers):
    upload_id = str(uuid.uuid4())
    path = f"/v1/user/apply/report-uploads/{upload_id}"
    started = client.post(
        "/v1/user/apply/report-uploads",
        json={"id": upload_id, "url": "https://x.test/apply", "field_count": 0, "chunk_count": 1},
        headers=user_headers,
    )
    assert started.status_code == 201
    oversized = client.put(f"{path}/chunks/0", content=b"x" * 65_537, headers=user_headers)
    assert oversized.status_code == 413
    assert client.get(path, headers=user_headers).json()["received_chunks"] == 0


def test_resumes_per_person_are_bounded_because_each_carries_its_pdf(client, user_headers):
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('resumes_per_user', '2') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    for name in ("one", "two"):
        made = client.post(
            "/v1/user/resumes", json={"name": name, "text": "a resume"}, headers=user_headers
        )
        assert made.status_code == 201, made.text
    # Replacing one by name is not a third.
    assert (
        client.post(
            "/v1/user/resumes", json={"name": "two", "text": "newer"}, headers=user_headers
        ).status_code
        == 201
    )
    refused = client.post(
        "/v1/user/resumes", json={"name": "three", "text": "a resume"}, headers=user_headers
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "TOO_MANY_RESUMES"


def test_a_submit_moves_the_board_row_without_a_reload(client, user_headers, monkeypatch):
    """The extension's submit writes a status the way the board's own patch
    does, and an open board hears about it on the person's channel, since a
    status written at submit time is not a task and the task events never
    carried it."""
    from api import events

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(events, "_publish", lambda channel, data: published.append((channel, data)))
    uid = _uid(user_headers)
    job_id = _insert_job("src-rt", "https://jobs.ashbyhq.com/rt/abc")
    fill = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://jobs.ashbyhq.com/rt/abc/application",
            "fields": [{"key": "k", "label": "Name"}],
        },
        headers=user_headers,
    ).json()
    client.post(
        f"/v1/user/apply/fills/{fill['fill_id']}/submitted",
        json={"fields": []},
        headers=user_headers,
    )
    rows = [
        d for c, d in published if c == f"jobtracker:user.{uid}" and d.get("type") == "board_row"
    ]
    assert rows and rows[-1]["job_id"] == job_id and rows[-1]["status"] == "Application Submitted"
    assert rows[-1]["date_applied"] and rows[-1]["hidden"] is False


def test_submission_retry_keeps_original_receipt_and_later_board_status(client, user_headers):
    uid = _uid(user_headers)
    job_id = _insert_job("retry", "https://jobs.ashbyhq.com/retry/abc")
    fill = client.post(
        "/v1/user/apply/resolve",
        json={"url": "https://jobs.ashbyhq.com/retry/abc/application", "fields": []},
        headers=user_headers,
    ).json()
    url = f"/v1/user/apply/fills/{fill['fill_id']}/submitted"
    assert client.post(url, json={"fields": []}, headers=user_headers).status_code == 200
    before = db.query_one(
        "SELECT submitted_at FROM application_fills WHERE id = %s", (fill["fill_id"],)
    )
    db.execute(
        "UPDATE user_jobs SET status = 'Interview' WHERE user_id = %s AND job_id = %s",
        (uid, job_id),
    )
    assert client.post(url, json={"fields": []}, headers=user_headers).status_code == 200
    assert (
        db.query_one("SELECT submitted_at FROM application_fills WHERE id = %s", (fill["fill_id"],))
        == before
    )
    assert (
        db.query_one(
            "SELECT status FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, job_id)
        )["status"]
        == "Interview"
    )


def test_submission_failure_rolls_back_receipt_and_saved_answers(client, user_headers, monkeypatch):
    from api.routers import apply as router

    uid = _uid(user_headers)
    _insert_job("atomic", "https://jobs.ashbyhq.com/atomic/abc")
    fill = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://jobs.ashbyhq.com/atomic/abc/application",
            "fields": [{"key": "q", "label": "Office preference", "kind": "text"}],
        },
        headers=user_headers,
    ).json()

    def fail(*args, **kwargs):
        raise RuntimeError("board unavailable")

    monkeypatch.setattr(router, "_write_board_row", fail)
    import pytest

    with pytest.raises(RuntimeError, match="board unavailable"):
        client.post(
            f"/v1/user/apply/fills/{fill['fill_id']}/submitted",
            json={"fields": [{"key": "q", "final": "Remote", "remember": True}]},
            headers=user_headers,
        )
    assert (
        db.query_one(
            "SELECT submitted_at FROM application_fills WHERE id = %s", (fill["fill_id"],)
        )["submitted_at"]
        is None
    )
    assert not db.query("SELECT id FROM application_answer_bank WHERE user_id = %s", (uid,))


def test_a_submit_records_the_application_it_made(client, user_headers):
    """`applications` is the one record that a person applied. A submit on a
    posting on the board links that posting's application; a submit on a
    form whose posting is not on the board makes one of provenance `apply`
    (8 such submits on production on 2026-10-10 had none)."""
    uid = _uid(user_headers)
    job_id = _insert_job("linked", "https://jobs.ashbyhq.com/linked/abc")
    for url in (
        "https://jobs.ashbyhq.com/linked/abc/application",
        "https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=1",
    ):
        fill = client.post(
            "/v1/user/apply/resolve", json={"url": url, "fields": []}, headers=user_headers
        ).json()
        resp = client.post(
            f"/v1/user/apply/fills/{fill['fill_id']}/submitted",
            json={"fields": []},
            headers=user_headers,
        )
        assert resp.status_code == 200, resp.text

    rows = db.query(
        "SELECT a.job_id, a.company_name, a.source_provenance, f.submitted_at, a.applied_at "
        "FROM application_fills f JOIN applications a ON a.id = f.application_id "
        "WHERE f.user_id = %s ORDER BY f.id",
        (uid,),
    )
    assert [(r["job_id"], r["source_provenance"]) for r in rows] == [
        (job_id, "tracker"),
        (None, "apply"),
    ]
    assert rows[1]["company_name"] == "stripe"
    assert rows[1]["applied_at"] == rows[1]["submitted_at"]
    assert (
        db.query_one("SELECT count(*) AS n FROM applications WHERE user_id = %s", (uid,))["n"] == 2
    )
