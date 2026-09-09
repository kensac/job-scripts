"""GET /admin/config serves the registry beside the values: type, help and
choices per key, so the admin page renders any tunable without a frontend
entry, and a new key is one entry in api.config.CONFIG_KEYS."""

from __future__ import annotations

from api import db
from api.routers import admin


def test_every_key_is_served_with_its_type_help_and_choices(client, admin_headers):
    body = client.get("/v1/admin/config", headers=admin_headers).json()
    assert set(body["keys"]) == set(admin._CONFIG_KEYS)
    for key, spec in body["keys"].items():
        assert spec["type"] in {"bool", "int", "str", "list", "dict"}, key
        assert spec["help"].strip(), key
    assert body["keys"]["fetch_engine"]["choices"] == ["chromium", "static_first"]
    assert body["keys"]["batch_straggler_hours"] == {
        "type": "int",
        "help": admin._CONFIG_KEYS["batch_straggler_hours"].help,
        "choices": [],
    }


def test_every_seeded_key_is_in_the_registry_and_the_reverse():
    seeded = {key for key, _ in db._APP_CONFIG_SEED}
    assert seeded == set(admin._CONFIG_KEYS)


def test_seeded_layout_round_trips_through_admin(client, admin_headers):
    layout = db.get_config("board_default_column_layout")
    response = client.put(
        "/v1/admin/config/board_default_column_layout",
        headers=admin_headers,
        json={"value": layout},
    )
    assert response.status_code == 200, response.text
    assert db.get_config("board_default_column_layout") == layout


def test_unrestricted_text_can_be_written(client, admin_headers):
    response = client.put(
        "/v1/admin/config/application_draft_instructions",
        headers=admin_headers,
        json={"value": "Use the person's experience."},
    )
    assert response.status_code == 200, response.text
    assert db.get_config("application_draft_instructions") == "Use the person's experience."


def test_host_limits_reject_boolean_rates_without_coercion(client, admin_headers):
    response = client.put(
        "/v1/admin/config/fetch_host_limits",
        headers=admin_headers,
        json={"value": {"example.com": True}},
    )
    assert response.status_code == 400, response.text
    assert db.get_config("fetch_host_limits") == {}


def test_malformed_stored_group_flags_fail_closed():
    from api import oauth
    from api.auth import AuthedUser
    from api.routers import filters

    for key in ("gmail_connect_groups", "filter_rejudge_on_change_groups"):
        db.execute("UPDATE app_config SET value = %s WHERE key = %s", (db.jsonb([{}]), key))
    assert oauth.connect_allowed(["infra-admins"]) is False
    assert (
        filters._rejudge_on_change(
            AuthedUser(
                id=1, sub="test", name="Test", email="test@example.com", groups=["infra-admins"]
            )
        )
        is False
    )


def test_generic_mapping_preserves_case_and_whitespace():
    from api.config import ConfigKey

    spec = ConfigKey(default={}, value_type=dict[str, int], help="Named counters")
    assert spec.validate({"MixedCase": 2, " spaced ": 3}) == {"MixedCase": 2, " spaced ": 3}


def test_generic_string_list_accepts_empty_strings():
    from api.config import ConfigKey

    spec = ConfigKey(default=[], value_type=list[str], help="Text fragments")
    assert spec.validate(["", " "]) == ["", " "]


def test_group_access_refuses_a_non_policy_string_list(monkeypatch):
    import pytest

    from api.config import CONFIG_KEYS, ConfigKey, group_access_allowed

    monkeypatch.setitem(
        CONFIG_KEYS, "text_fragments", ConfigKey(default=[], value_type=list[str], help="Text")
    )
    db.execute(
        "INSERT INTO app_config (key, value) VALUES (%s, %s)",
        ("text_fragments", db.jsonb(["*"])),
    )
    with pytest.raises(ValueError, match="not a group policy"):
        group_access_allowed("text_fragments", ["infra-admins"])
