from __future__ import annotations

import datetime
import os

from api import db
from core.store import add_ai_result

SERVICE_TOKEN = os.environ["JOBTRACKER_SERVICE_TOKEN"]


def _uid(headers: dict) -> int:
    return db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))["id"]


def _insert_job(
    source: str,
    url: str,
    active: bool = True,
    uploaded_by=None,
    locations=None,
    terms=None,
    date_posted=None,
    company: str = "Acme",
    title: str = "Engineer",
) -> int:
    row = db.query_one(
        """
        INSERT INTO jobs (url, raw_url, company, title, locations, terms, source, active, date_posted, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (url, url, company, title, locations or [], terms or [], source, active, date_posted, uploaded_by),
    )
    return row["id"]


def _subscribe(uid: int, source: str) -> None:
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (uid, source),
    )


def _pass_closed(url: str) -> None:
    add_ai_result(url, "passed", check_type="closed")


def _job_ids(payload: dict) -> set:
    return {r["job_id"] for r in payload["rows"]}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_missing_service_token_is_401(client):
    resp = client.get("/v1/user/jobs")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"


def test_wrong_service_token_is_401(client):
    resp = client.get("/v1/user/jobs", headers={"X-Service-Token": "definitely-wrong"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"


def test_valid_token_missing_user_headers_is_401(client):
    resp = client.get("/v1/user/jobs", headers={"X-Service-Token": SERVICE_TOKEN})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# GET /v1/user/jobs visibility matrix
# ---------------------------------------------------------------------------


def test_subscribed_closed_passed_no_filters_is_visible(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-a", "https://x.test/a1")
    _subscribe(uid, "src-a")
    _pass_closed("https://x.test/a1")

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert resp.status_code == 200
    assert jid in _job_ids(resp.json())


def test_unsubscribed_source_invisible_then_user_jobs_row_overrides(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-b", "https://x.test/b1")
    _pass_closed("https://x.test/b1")

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid not in _job_ids(resp.json())

    patch = client.patch(f"/v1/user/jobs/{jid}", json={"notes": "watching"}, headers=user_headers)
    assert patch.status_code == 200

    resp2 = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp2.json())


def test_enabled_filter_gates_visibility_disabled_bypasses(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-c", "https://x.test/c1")
    _subscribe(uid, "src-c")
    _pass_closed("https://x.test/c1")

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())

    created = client.post(
        "/v1/user/filters", json={"name": "must-be-remote", "prompt": "must be remote"}, headers=user_headers
    )
    assert created.status_code == 200
    filt = created.json()
    prompt_hash = filt["prompt_hash"]
    filter_id = filt["id"]

    # enabled filter, no verdict yet -> invisible
    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid not in _job_ids(resp.json())

    add_ai_result("https://x.test/c1", "rejected", check_type="custom", prompt_hash=prompt_hash)
    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid not in _job_ids(resp.json())

    add_ai_result("https://x.test/c1", "passed", check_type="custom", prompt_hash=prompt_hash)
    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())

    # disabling the filter makes the job visible regardless of verdict
    patch = client.patch(f"/v1/user/filters/{filter_id}", json={"enabled": False}, headers=user_headers)
    assert patch.status_code == 200
    add_ai_result("https://x.test/c1", "rejected", check_type="custom", prompt_hash=prompt_hash)
    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())


def test_criteria_excluded_locations_word_boundary(client, user_headers):
    uid = _uid(user_headers)
    jid_uk = _insert_job("src-d", "https://x.test/d1", locations=["London, UK"])
    jid_wa = _insert_job("src-d", "https://x.test/d2", locations=["Tukwila, WA"])
    _subscribe(uid, "src-d")
    _pass_closed("https://x.test/d1")
    _pass_closed("https://x.test/d2")

    put = client.put(
        "/v1/user/settings", json={"criteria": {"excluded_locations": ["UK"]}}, headers=user_headers
    )
    assert put.status_code == 200

    resp = client.get("/v1/user/jobs", headers=user_headers)
    ids = _job_ids(resp.json())
    assert jid_uk not in ids
    assert jid_wa in ids


def test_criteria_date_posted_after_hides_older(client, user_headers):
    uid = _uid(user_headers)
    old_id = _insert_job("src-e", "https://x.test/e1", date_posted=datetime.date(2024, 1, 1))
    new_id = _insert_job("src-e", "https://x.test/e2", date_posted=datetime.date(2024, 8, 1))
    _subscribe(uid, "src-e")
    _pass_closed("https://x.test/e1")
    _pass_closed("https://x.test/e2")

    put = client.put(
        "/v1/user/settings", json={"criteria": {"date_posted_after": "2024-06-01"}}, headers=user_headers
    )
    assert put.status_code == 200

    resp = client.get("/v1/user/jobs", headers=user_headers)
    ids = _job_ids(resp.json())
    assert old_id not in ids
    assert new_id in ids


def test_uploaded_by_user_always_visible(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("upload", "https://x.test/f1", active=False, uploaded_by=uid)

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())


def test_statuses_csv_filter_including_not_applied_sentinel(client, user_headers):
    uid = _uid(user_headers)
    j_none = _insert_job("src-g", "https://x.test/g1")
    j_applied = _insert_job("src-g", "https://x.test/g2")
    j_interviewing = _insert_job("src-g", "https://x.test/g3")
    _subscribe(uid, "src-g")
    for url in ("https://x.test/g1", "https://x.test/g2", "https://x.test/g3"):
        _pass_closed(url)

    client.patch(f"/v1/user/jobs/{j_applied}", json={"status": "applied"}, headers=user_headers)
    client.patch(f"/v1/user/jobs/{j_interviewing}", json={"status": "interviewing"}, headers=user_headers)

    resp = client.get(
        "/v1/user/jobs", params={"statuses": "applied,not_applied"}, headers=user_headers
    )
    ids = _job_ids(resp.json())
    assert j_none in ids
    assert j_applied in ids
    assert j_interviewing not in ids


def test_bogus_sort_param_does_not_500(client, user_headers):
    resp = client.get("/v1/user/jobs", params={"sort": "not-a-real-column"}, headers=user_headers)
    assert resp.status_code == 200


def test_offset_limit_with_total(client, user_headers):
    uid = _uid(user_headers)
    for i in range(5):
        url = f"https://x.test/h{i}"
        _insert_job("src-h", url)
        _pass_closed(url)
    _subscribe(uid, "src-h")

    resp = client.get(
        "/v1/user/jobs", params={"limit": 2, "offset": 0, "with_total": "true"}, headers=user_headers
    )
    data = resp.json()
    assert data["total"] == 5
    assert len(data["rows"]) == 2
    assert data["has_more"] is True

    resp2 = client.get(
        "/v1/user/jobs", params={"limit": 2, "offset": 4, "with_total": "true"}, headers=user_headers
    )
    data2 = resp2.json()
    assert data2["total"] == 5
    assert len(data2["rows"]) == 1
    assert data2["has_more"] is False


def test_delete_user_job_removes_only_user_jobs_row(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-i", "https://x.test/i1")
    client.patch(f"/v1/user/jobs/{jid}", json={"notes": "x"}, headers=user_headers)
    assert db.query_one("SELECT 1 FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))

    resp = client.delete(f"/v1/user/jobs/{jid}", headers=user_headers)
    assert resp.status_code == 200
    assert db.query_one("SELECT 1 FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid)) is None
    assert db.query_one("SELECT 1 FROM jobs WHERE id = %s", (jid,)) is not None


def test_patch_status_autofills_date_applied(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af1")
    resp = client.patch(f"/v1/user/jobs/{jid}", json={"status": "Applied"}, headers=user_headers)
    assert resp.status_code == 200
    today = datetime.date.today()
    assert resp.json()["autofilled"] == {"date_applied": today.isoformat()}
    row = db.query_one("SELECT date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))
    assert row["date_applied"] == today


def test_patch_explicit_date_applied_wins_over_autofill(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af2")
    resp = client.patch(
        f"/v1/user/jobs/{jid}",
        json={"status": "Applied", "date_applied": "2026-08-01"},
        headers=user_headers,
    )
    assert resp.json()["autofilled"] == {}
    row = db.query_one("SELECT date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))
    assert row["date_applied"] == datetime.date(2026, 8, 1)


def test_patch_status_change_does_not_overwrite_existing_date_applied(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af3")
    client.patch(
        f"/v1/user/jobs/{jid}",
        json={"status": "Applied", "date_applied": "2026-08-01"},
        headers=user_headers,
    )
    resp = client.patch(f"/v1/user/jobs/{jid}", json={"status": "Interview"}, headers=user_headers)
    assert resp.json()["autofilled"] == {}
    row = db.query_one("SELECT date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))
    assert row["date_applied"] == datetime.date(2026, 8, 1)


def test_patch_notes_only_does_not_autofill_date_applied(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-af", "https://x.test/af4")
    resp = client.patch(f"/v1/user/jobs/{jid}", json={"notes": "check later"}, headers=user_headers)
    assert resp.json()["autofilled"] == {}
    row = db.query_one("SELECT date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))
    assert row["date_applied"] is None


def test_status_changes_append_history(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-h", "https://x.test/h1")
    client.patch(f"/v1/user/jobs/{jid}", json={"status": "Applied"}, headers=user_headers)
    client.patch(f"/v1/user/jobs/{jid}", json={"status": "Interview"}, headers=user_headers)
    client.patch(f"/v1/user/jobs/{jid}", json={"notes": "n"}, headers=user_headers)
    rows = db.query(
        "SELECT old_status, new_status FROM user_job_history WHERE user_id = %s AND job_id = %s ORDER BY id",
        (uid, jid),
    )
    assert [(r["old_status"], r["new_status"]) for r in rows] == [
        (None, "Applied"),
        ("Applied", "Interview"),
    ]


def test_job_detail_returns_content_verdicts_history(client, user_headers):
    uid = _uid(user_headers)
    jid = _insert_job("src-d", "https://x.test/d1", company="Acme", title="SWE")
    add_ai_result("https://x.test/d1", "passed", "content cached", "content", input_content="THE JOB TEXT")
    add_ai_result("https://x.test/d1", "passed", "job open", "closed")
    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash) VALUES (%s, 'f1', 'p', 'hash1')",
        (uid,),
    )
    add_ai_result("https://x.test/d1", "passed", "matches profile", "custom", prompt_hash="hash1")
    client.patch(f"/v1/user/jobs/{jid}", json={"status": "Applied"}, headers=user_headers)
    resp = client.get(f"/v1/user/jobs/{jid}/detail", headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "THE JOB TEXT"
    assert body["row"]["status"] == "Applied"
    assert body["history"][0]["new_status"] == "Applied"
    assert {c["check_type"]: c["status"] for c in body["checks"]} == {"closed": "passed"}
    assert body["filter_verdicts"][0]["reason"] == "matches profile"


def test_job_options_serves_canon_plus_in_use(client, user_headers):
    jid = _insert_job("src-opt", "https://x.test/opt1")
    client.patch(f"/v1/user/jobs/{jid}", json={"status": "Weird Legacy State"}, headers=user_headers)
    db.execute("INSERT INTO sources (name, listings_url) VALUES ('src-opt', 'https://x') ON CONFLICT DO NOTHING")
    uid = _uid(user_headers)
    db.execute("INSERT INTO user_sources (user_id, source) VALUES (%s, 'src-opt') ON CONFLICT DO NOTHING", (uid,))
    resp = client.get("/v1/user/jobs/options", headers=user_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "Application Submitted" in body["statuses"]
    assert "Weird Legacy State" in body["statuses"]
    assert body["statuses"].index("Weird Legacy State") > body["statuses"].index("Rejected")
    assert body["not_applied_sentinel"] == "not_applied"
    assert "src-opt" in body["sources"]
