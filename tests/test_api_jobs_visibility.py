from __future__ import annotations

import datetime

from api import db
from core.store import add_ai_result
from tests.factories import user_id_for_subject
from tests.job_board_support import _insert_job, _job_ids, _pass_closed, _subscribe


def test_subscribed_closed_passed_no_filters_is_visible(client, user_headers):
    uid = user_id_for_subject("test-user")
    jid = _insert_job("src-a", "https://x.test/a1")
    _subscribe(uid, "src-a")
    _pass_closed("https://x.test/a1")

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert resp.status_code == 200
    assert jid in _job_ids(resp.json())


def test_unsubscribed_source_invisible_then_user_jobs_row_overrides(client, user_headers):
    user_id_for_subject("test-user")
    jid = _insert_job("src-b", "https://x.test/b1")
    _pass_closed("https://x.test/b1")

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid not in _job_ids(resp.json())

    patch = client.patch(f"/v1/user/jobs/{jid}", json={"notes": "watching"}, headers=user_headers)
    assert patch.status_code == 200

    resp2 = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp2.json())


def test_enabled_filter_gates_visibility_disabled_bypasses(client, user_headers):
    uid = user_id_for_subject("test-user")
    jid = _insert_job("src-c", "https://x.test/c1")
    _subscribe(uid, "src-c")
    _pass_closed("https://x.test/c1")

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())

    created = client.post(
        "/v1/user/filters",
        json={"name": "must-be-remote", "prompt": "must be remote"},
        headers=user_headers,
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
    patch = client.patch(
        f"/v1/user/filters/{filter_id}", json={"enabled": False}, headers=user_headers
    )
    assert patch.status_code == 200
    add_ai_result("https://x.test/c1", "rejected", check_type="custom", prompt_hash=prompt_hash)
    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())


def test_criteria_excluded_locations_match_places(client, user_headers):
    from tasks.derivations import locations

    uid = user_id_for_subject("test-user")
    jid_uk = _insert_job("src-d", "https://x.test/d1", locations=["London, UK"])
    jid_wa = _insert_job("src-d", "https://x.test/d2", locations=["Tukwila, WA"])
    _subscribe(uid, "src-d")
    _pass_closed("https://x.test/d1")
    _pass_closed("https://x.test/d2")
    locations.store("London, UK", locations.LocationExtract(country="GB", city="London"), "t")
    locations.store("Tukwila, WA", locations.LocationExtract(country="US", region="WA"), "t")
    locations.store("UK", locations.LocationExtract(country="GB"), "t")

    put = client.put(
        "/v1/user/settings", json={"criteria": {"excluded_locations": ["UK"]}}, headers=user_headers
    )
    assert put.status_code == 200

    resp = client.get("/v1/user/jobs", headers=user_headers)
    ids = _job_ids(resp.json())
    assert jid_uk not in ids
    assert jid_wa in ids


def test_criteria_date_posted_after_hides_older(client, user_headers):
    # Dates inside the 30-day window a non-admin's criteria carry by default,
    # so this exercises the date criterion alone.
    uid = user_id_for_subject("test-user")
    today = datetime.date.today()
    old_id = _insert_job(
        "src-e", "https://x.test/e1", date_posted=today - datetime.timedelta(days=20)
    )
    new_id = _insert_job(
        "src-e", "https://x.test/e2", date_posted=today - datetime.timedelta(days=5)
    )
    _subscribe(uid, "src-e")
    _pass_closed("https://x.test/e1")
    _pass_closed("https://x.test/e2")

    put = client.put(
        "/v1/user/settings",
        json={"criteria": {"date_posted_after": (today - datetime.timedelta(days=10)).isoformat()}},
        headers=user_headers,
    )
    assert put.status_code == 200

    resp = client.get("/v1/user/jobs", headers=user_headers)
    ids = _job_ids(resp.json())
    assert old_id not in ids
    assert new_id in ids


def test_criteria_max_age_days_is_a_rolling_window_with_a_catalog_fallback(client, user_headers):
    """A fixed date has to be moved by hand; the window moves every day. A
    posting the board never dated ages from the day the catalog first saw
    it, so it still expires; an acted-on row does not."""
    uid = user_id_for_subject("test-user")
    today = datetime.date.today()
    fresh = _insert_job(
        "src-age", "https://x.test/a1", date_posted=today - datetime.timedelta(days=5)
    )
    stale = _insert_job(
        "src-age", "https://x.test/a2", date_posted=today - datetime.timedelta(days=40)
    )
    undated = _insert_job("src-age", "https://x.test/a3", date_posted=None)
    stale_applied = _insert_job(
        "src-age", "https://x.test/a4", date_posted=today - datetime.timedelta(days=40)
    )
    _subscribe(uid, "src-age")
    for i in range(1, 5):
        _pass_closed(f"https://x.test/a{i}")
    db.execute("UPDATE jobs SET created_at = now() - interval '45 days' WHERE id = %s", (undated,))
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, 'Applied')",
        (uid, stale_applied),
    )
    assert _job_ids(client.get("/v1/user/jobs", headers=user_headers).json()) >= {
        fresh,
        stale,
        undated,
        stale_applied,
    }

    put = client.put(
        "/v1/user/settings", json={"criteria": {"max_age_days": 30}}, headers=user_headers
    )
    assert put.status_code == 200, put.text
    ids = _job_ids(client.get("/v1/user/jobs", headers=user_headers).json())
    assert fresh in ids and stale_applied in ids
    assert stale not in ids and undated not in ids
    # Out of range is refused, not clamped.
    assert (
        client.put(
            "/v1/user/settings", json={"criteria": {"max_age_days": 0}}, headers=user_headers
        ).status_code
        == 422
    )


def test_uploaded_by_user_always_visible(client, user_headers):
    uid = user_id_for_subject("test-user")
    jid = _insert_job("upload", "https://x.test/f1", active=False, uploaded_by=uid)

    resp = client.get("/v1/user/jobs", headers=user_headers)
    assert jid in _job_ids(resp.json())
