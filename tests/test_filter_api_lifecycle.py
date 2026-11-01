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


# A person runs one filter (Kanishk, 2026-09-08: a new account's three filters
# judged the whole catalog three times over). A second enabled filter is
# refused on create, on turning it on, and on adopting a preset; a disabled
# second is fine, and turning the first off frees the slot.


def _create_filter(client, user_headers, name: str, prompt: str, **extra):
    return client.post(
        "/v1/user/filters", json={"name": name, "prompt": prompt, **extra}, headers=user_headers
    )


def _set_enabled(client, user_headers, filter_id: int, enabled: bool, **extra):
    return client.patch(
        f"/v1/user/filters/{filter_id}", json={"enabled": enabled, **extra}, headers=user_headers
    )


def _enabled_filter(client, user_headers) -> dict:
    first = _create_filter(client, user_headers, "one", "remote only").json()
    assert first["enabled"] is True
    return first


def _parked_filter(client, user_headers) -> dict:
    parked = _create_filter(client, user_headers, "two", "pays well", enabled=False).json()
    assert parked["enabled"] is False
    return parked


def test_a_second_enabled_filter_is_refused_on_create_naming_the_one_on(client, user_headers):
    first = _enabled_filter(client, user_headers)

    second = _create_filter(client, user_headers, "two", "pays well")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "ONE_FILTER"
    assert second.json()["detail"]["enabled_filter_id"] == first["id"]
    assert '"one" is on' in second.json()["detail"]["message"]
    assert "fold this into it" in second.json()["detail"]["message"]


def test_turning_on_a_disabled_second_filter_is_refused(client, user_headers):
    _enabled_filter(client, user_headers)
    parked = _parked_filter(client, user_headers)

    turn_on = _set_enabled(client, user_headers, parked["id"], True)
    assert turn_on.status_code == 409 and turn_on.json()["detail"]["code"] == "ONE_FILTER"


def test_editing_the_enabled_filter_is_not_a_second(client, user_headers):
    first = _enabled_filter(client, user_headers)
    _parked_filter(client, user_headers)

    edit = _set_enabled(client, user_headers, first["id"], True, prompt="remote only, please")
    assert edit.status_code == 200


def test_adopting_a_preset_while_a_filter_is_on_is_refused(client, user_headers, admin_headers):
    _enabled_filter(client, user_headers)
    _parked_filter(client, user_headers)
    preset_id = client.post(
        "/v1/admin/filter-presets",
        json={"name": "Remote Only", "prompt": "must be fully remote"},
        headers=admin_headers,
    ).json()["id"]

    adopt = client.post(f"/v1/filter-presets/{preset_id}/adopt", headers=user_headers)
    assert adopt.status_code == 409 and adopt.json()["detail"]["code"] == "ONE_FILTER"


def test_turning_the_enabled_filter_off_frees_the_slot(client, user_headers):
    first = _enabled_filter(client, user_headers)
    parked = _parked_filter(client, user_headers)

    assert _set_enabled(client, user_headers, first["id"], False).status_code == 200
    assert _set_enabled(client, user_headers, parked["id"], True).status_code == 200


def test_a_filter_save_waits_for_the_sweep_unless_the_group_rejudges_on_change(
    client, user_headers
):
    """Seeded closed: a save returns DEFERRED with the sentence the page shows
    and queues nothing; the hourly sweep judges the board. With "*" in
    filter_rejudge_on_change_groups the save queues the run as before, or
    names the budget (Kanishk, 2026-09-09)."""
    from api.routers.filtering.run_controls import DEFERRED_MESSAGE, REJUDGE_GROUPS_KEY

    def gate(groups):
        db.execute(
            "INSERT INTO app_config (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (REJUDGE_GROUPS_KEY, db.jsonb(groups)),
        )

    try:
        gate([])
        deferred = client.post(
            "/v1/user/filters",
            json={"name": "deferred", "prompt": "robotics only"},
            headers=user_headers,
        ).json()
        assert deferred["task_id"] is None and deferred["run_blocked"] == "DEFERRED"
        assert deferred["run_blocked_message"] == DEFERRED_MESSAGE
        edited = client.patch(
            f"/v1/user/filters/{deferred['id']}",
            json={"prompt": "robotics or embedded only"},
            headers=user_headers,
        ).json()
        assert edited["task_id"] is None and edited["run_blocked"] == "DEFERRED"
        assert not db.query_one(
            "SELECT 1 FROM tasks WHERE kind = 'run_filter' "
            "AND (payload->>'filter_id')::bigint = %s",
            (deferred["id"],),
        )

        gate(["*"])
        at_once = client.patch(
            f"/v1/user/filters/{deferred['id']}",
            json={"prompt": "embedded only"},
            headers=user_headers,
        ).json()
        assert at_once["run_blocked"] != "DEFERRED"
        assert at_once["task_id"] is not None or at_once["run_blocked"] is not None
    finally:
        gate([])
