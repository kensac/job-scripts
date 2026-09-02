from __future__ import annotations

from api import db
from core.filters import build_custom_instructions, compute_prompt_hash


def test_create_filter_returns_task_id_or_run_blocked_and_stores_prompt_hash(client, user_headers):
    resp = client.post(
        "/v1/user/filters",
        json={"name": "visa-sponsor", "prompt": "must sponsor visas"},
        headers=user_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert ("task_id" in body) and ("run_blocked" in body)
    assert body["task_id"] is not None or body["run_blocked"] is not None

    expected_hash = compute_prompt_hash(build_custom_instructions("must sponsor visas", "keep"))
    assert body["prompt_hash"] == expected_hash

    row = db.query_one("SELECT prompt_hash FROM user_filters WHERE id = %s", (body["id"],))
    assert row["prompt_hash"] == expected_hash


def test_duplicate_filter_name_for_same_user_is_conflict(client, user_headers):
    first = client.post(
        "/v1/user/filters", json={"name": "dup-name", "prompt": "prompt one"}, headers=user_headers
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/user/filters", json={"name": "dup-name", "prompt": "prompt two"}, headers=user_headers
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "DUPLICATE_NAME"


def test_presets_list_returns_seeded_presets(client, admin_headers, user_headers):
    created = client.post(
        "/v1/admin/filter-presets",
        json={"name": "Remote Only", "prompt": "must be fully remote"},
        headers=admin_headers,
    )
    assert created.status_code == 200
    preset = created.json()

    listed = client.get("/v1/filter-presets", headers=user_headers)
    assert listed.status_code == 200
    names = {p["name"] for p in listed.json()["presets"]}
    assert preset["name"] in names


def test_adopt_preset_creates_user_filter(client, admin_headers, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE sub = %s", (user_headers["X-User-Sub"],))["id"]
    created = client.post(
        "/v1/admin/filter-presets",
        json={"name": "Junior Friendly", "prompt": "no more than 2 years experience required"},
        headers=admin_headers,
    )
    preset_id = created.json()["id"]

    adopted = client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers)
    assert adopted.status_code == 200
    body = adopted.json()
    assert body["name"] == "Junior Friendly"
    assert body["enabled"] is True

    row = db.query_one(
        "SELECT id FROM user_filters WHERE user_id = %s AND name = %s", (uid, "Junior Friendly")
    )
    assert row is not None

    # adopting the same preset twice is rejected
    again = client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ALREADY_ADOPTED"
