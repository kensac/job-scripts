"""GET /user/models lists every model with whether this caller can run it and,
if not, why, instead of a list that silently shrank."""

from __future__ import annotations

from api import crypto, db
from api.routers.users import NEEDS_OWN_KEY, NOT_ON_ALLOWLIST


def _by_provider(body: dict) -> dict[str, dict[str, dict]]:
    return {p["provider"]: {m["model"]: m for m in p["models"]} for p in body["providers"]}


def _user_id() -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")
    assert row is not None
    return row["id"]


def test_without_any_key_everything_is_listed_and_nothing_is_eligible(
    client, user_headers, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = client.get("/v1/models", headers=user_headers).json()
    models = _by_provider(body)
    assert models.get("openai")
    assert all(
        m["eligible"] is False and m["reason"] == NEEDS_OWN_KEY
        for provider in models.values()
        for m in provider.values()
    )


def test_on_the_owner_key_only_the_allowlist_is_eligible_and_the_rest_say_why(
    client, user_headers, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    uid = _user_id()
    row = db.query_one("SELECT groups FROM users WHERE id = %s", (uid,))
    assert row is not None and row["groups"]
    db.execute(
        "INSERT INTO group_budgets (group_name, weekly_token_budget, allowed_models) "
        "VALUES (%s, 1000000, ARRAY['gpt-5-nano']) ON CONFLICT (group_name) DO UPDATE "
        "SET allowed_models = EXCLUDED.allowed_models, weekly_token_budget = EXCLUDED.weekly_token_budget",
        (row["groups"][0],),
    )
    body = client.get("/v1/models", headers=user_headers).json()
    models = _by_provider(body)
    openai = models["openai"]
    assert openai["gpt-5-nano"]["eligible"] is True and openai["gpt-5-nano"]["reason"] is None
    others = [m for name, m in openai.items() if name != "gpt-5-nano"]
    assert others and all(
        m["eligible"] is False and m["reason"] == NOT_ON_ALLOWLIST for m in others
    )
    # A provider the server holds no key for cannot be unlocked by the allowlist.
    for provider, entries in models.items():
        if provider == "openai":
            continue
        assert all(m["reason"] == NEEDS_OWN_KEY for m in entries.values()), provider
    assert body["owner_key_models"] == ["gpt-5-nano"]


def test_a_byo_key_makes_its_providers_whole_catalog_eligible(client, user_headers):
    uid = _user_id()
    db.execute(
        "INSERT INTO user_settings (user_id, ai_provider, api_key_enc) VALUES (%s, 'openai', %s) "
        "ON CONFLICT (user_id) DO UPDATE SET ai_provider = EXCLUDED.ai_provider, "
        "api_key_enc = EXCLUDED.api_key_enc",
        (uid, crypto.encrypt("sk-byo")),
    )
    body = client.get("/v1/models", headers=user_headers).json()
    models = _by_provider(body)
    assert list(models) == ["openai"]
    assert all(m["eligible"] is True and m["reason"] is None for m in models["openai"].values())
