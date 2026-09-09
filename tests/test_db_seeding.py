from __future__ import annotations

import pytest
from psycopg.errors import NotNullViolation

from api import db


def test_seed_defaults_restores_missing_rows_and_preserves_overrides():
    db.execute("DELETE FROM group_budgets")
    db.execute("DELETE FROM app_config")
    db.execute("INSERT INTO app_config (key, value) VALUES ('signups_enabled', 'false')")
    db.execute(
        "INSERT INTO group_budgets (group_name, weekly_token_budget) VALUES ('infra-admins', 123)"
    )
    db.seed_defaults()
    db.seed_defaults()
    assert db.get_config("signups_enabled") is False
    assert db.get_config("gmail_connect_groups") == ["infra-admins"]
    assert db.get_config("requirements_extraction_enabled") is False
    assert db.query_one(
        "SELECT weekly_token_budget FROM group_budgets WHERE group_name = 'infra-admins'"
    ) == {"weekly_token_budget": 123}
    assert {r["key"] for r in db.query("SELECT key FROM app_config")} == {
        key for key, _ in db._APP_CONFIG_SEED
    }


def test_seed_defaults_rolls_back_both_tables_on_failure(monkeypatch):
    db.execute("DELETE FROM group_budgets")
    db.execute("DELETE FROM app_config")
    monkeypatch.setattr(db, "_APP_CONFIG_SEED", [*db._APP_CONFIG_SEED, (None, True)])
    with pytest.raises(NotNullViolation):
        db.seed_defaults()
    assert db.query("SELECT * FROM group_budgets") == []
    assert db.query("SELECT * FROM app_config") == []


def test_seed_defaults_participates_in_caller_transaction():
    db.execute("DELETE FROM group_budgets")
    db.execute("DELETE FROM app_config")
    with pytest.raises(RuntimeError, match="roll back"), db.transaction():
        db.seed_defaults()
        assert db.query_one(
            "SELECT weekly_token_budget FROM group_budgets WHERE group_name = 'infra-admins'"
        ) == {"weekly_token_budget": None}
        raise RuntimeError("roll back")
    assert db.query("SELECT * FROM group_budgets") == []
    assert db.query("SELECT * FROM app_config") == []
