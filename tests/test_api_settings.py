from __future__ import annotations


# ---------------------------------------------------------------------------
# PUT /v1/user/settings - ai_model / ai_params validation
# ---------------------------------------------------------------------------


def test_invalid_model_not_in_owner_allowlist_is_400(client, admin_headers, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")
    resp = client.put("/v1/user/settings", json={"ai_model": "not-a-real-model"}, headers=admin_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_MODEL"


def test_valid_model_from_owner_models_unlimited_group(client, admin_headers, monkeypatch):
    # infra-admins has weekly_token_budget=NULL -> unlimited owner-key policy,
    # so any keyed catalog model is allowed.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")
    resp = client.put("/v1/user/settings", json={"ai_model": "gpt-5-nano"}, headers=admin_headers)
    assert resp.status_code == 200


def test_budgeted_group_restricted_to_owner_key_models_default_policy(client, user_headers, monkeypatch):
    # jobtracker-users-internal has a finite weekly budget -> default policy is
    # JOBTRACKER_OWNER_KEY_MODELS (gpt-5-nano, gpt-5-mini by default), not the
    # full catalog.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")
    ok = client.put("/v1/user/settings", json={"ai_model": "gpt-5-nano"}, headers=user_headers)
    assert ok.status_code == 200

    blocked = client.put("/v1/user/settings", json={"ai_model": "gpt-5"}, headers=user_headers)
    assert blocked.status_code == 400
    assert blocked.json()["detail"]["code"] == "INVALID_MODEL"


def test_invalid_ai_params_for_provider_is_400(client, user_headers):
    # default provider is openai; temperature is only valid for openai_compatible.
    resp = client.put(
        "/v1/user/settings", json={"ai_params": {"temperature": 0.5}}, headers=user_headers
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_PARAMS"


# ---------------------------------------------------------------------------
# PUT /v1/user/settings/api-key
# ---------------------------------------------------------------------------


def test_api_key_openai_compatible_http_base_url_rejected(client, user_headers):
    resp = client.put(
        "/v1/user/settings/api-key",
        json={"api_key": "dummy-key-1", "provider": "openai_compatible", "base_url": "http://example.com"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_BASE_URL"


def test_api_key_openai_compatible_ip_literal_rejected(client, user_headers):
    resp = client.put(
        "/v1/user/settings/api-key",
        json={"api_key": "dummy-key-1", "provider": "openai_compatible", "base_url": "https://8.8.8.8/v1"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_BASE_URL"


def test_api_key_openai_compatible_missing_base_url_rejected(client, user_headers):
    resp = client.put(
        "/v1/user/settings/api-key",
        json={"api_key": "dummy-key-1", "provider": "openai_compatible"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "BASE_URL_REQUIRED"


def test_api_key_bogus_provider_rejected(client, user_headers):
    resp = client.put(
        "/v1/user/settings/api-key",
        json={"api_key": "dummy-key-1", "provider": "bogus-provider"},
        headers=user_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_PROVIDER"


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


def test_models_no_byo_key_reflects_owner_allowlist(client, admin_headers, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")
    resp = client.get("/v1/models", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["providers"], "expected at least one provider from the owner allowlist"
    providers = {p["provider"] for p in data["providers"]}
    assert providers == {"openai"}
    assert data["key_source"] == "owner"
    assert "gpt-5-nano" in data["owner_key_models"]


def test_models_after_byo_key_only_that_provider_catalog(client, user_headers):
    put = client.put(
        "/v1/user/settings/api-key",
        json={"api_key": "sk-test-byo-key", "provider": "openai"},
        headers=user_headers,
    )
    assert put.status_code == 200

    resp = client.get("/v1/models", headers=user_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["providers"]) == 1
    assert data["providers"][0]["provider"] == "openai"
    assert data["key_source"] == "byo"
