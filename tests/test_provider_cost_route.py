from unittest.mock import patch


def test_provider_costs_require_admin(client, user_headers):
    with patch("core.provider_costs.fetch_costs") as fetch:
        response = client.get("/v1/admin/spend/provider", headers=user_headers)
    assert response.status_code == 403
    fetch.assert_not_called()


def test_provider_costs_missing_secret_is_not_zero(client, admin_headers, monkeypatch):
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    response = client.get("/v1/admin/spend/provider?days=7", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_configured"
    assert body["total_usd"] is None
