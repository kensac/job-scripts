"""A seeded config key behaves the same whether or not the seed ran.

The seed runs after _migrate(), in a separate transaction and outside the
advisory lock, so a container that migrates and then dies leaves alembic
reporting head with the row absent. Every caller passing its own fallback then
silently gets that fallback. For a feature gate that reads as "the feature did
not ship" - no error, no exception, nothing a health check can see, and
indistinguishable from a deploy that never happened.
"""

from __future__ import annotations

from api import db, oauth


def _clear(key: str) -> None:
    db.execute("DELETE FROM app_config WHERE key = %s", (key,))


def test_seeded_key_falls_back_to_its_seeded_value():
    _clear("gmail_connect_groups")
    assert db.get_config("gmail_connect_groups") == ["infra-admins"]
    # A caller's own fallback must not win over the declared seed, which is the
    # bug: oauth.connect_allowed passes [] and would gate everyone out.
    assert db.get_config("gmail_connect_groups", []) == ["infra-admins"]


def test_stored_value_still_wins_over_the_seed():
    """Seeding is a default, not an override. Widening the gate through the
    admin endpoint must survive."""
    db.execute(
        "INSERT INTO app_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        ("gmail_connect_groups", db.jsonb(["everyone"])),
    )
    assert db.get_config("gmail_connect_groups") == ["everyone"]


def test_unseeded_key_still_uses_the_callers_default():
    _clear("no_such_config_key")
    assert db.get_config("no_such_config_key", "fallback") == "fallback"
    assert db.get_config("no_such_config_key") is None


def test_the_gate_holds_when_the_seed_never_ran():
    """The end-to-end consequence: an admin keeps access to a feature whose
    config row is missing, rather than the feature silently not existing."""
    _clear("gmail_connect_groups")
    assert oauth.connect_allowed(["infra-admins"]) is True
    assert oauth.connect_allowed(["jobtracker-users-internal"]) is False
