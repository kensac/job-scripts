"""GET /admin/config serves the registry beside the values: type, help and
choices per key, so the admin page renders any tunable without a frontend
entry, and a new key is one entry in api.routers.admin._CONFIG_KEYS."""

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
