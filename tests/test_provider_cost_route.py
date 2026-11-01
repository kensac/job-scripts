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


def _report(status):
    return {
        "provider": "openai",
        "basis": "provider_reported_cost",
        "status": status,
        "reason": None if status == "available" else "REQUEST_FAILED",
        "scope": "organization",
        "project_ids": [],
        "window_basis": "completed_utc_days",
        "start_time": 0,
        "end_time": 86400,
        "fetched_at": "2026-11-01T00:00:00+00:00",
        "total_usd": 1 if status == "available" else None,
        "daily": [],
    }


def test_provider_costs_are_served_stale_but_a_failure_is_not(client, admin_headers):
    """A failed read reaches the page as it is and the next visit asks again;
    an available one is served from the cache."""
    with patch("core.provider_costs.fetch_costs") as fetch:
        fetch.return_value = _report("unavailable")
        first = client.get("/v1/admin/spend/provider?days=7", headers=admin_headers).json()
        fetch.return_value = _report("available")
        second = client.get("/v1/admin/spend/provider?days=7", headers=admin_headers).json()
        third = client.get("/v1/admin/spend/provider?days=7", headers=admin_headers).json()
    assert (first["status"], second["status"], third["status"]) == (
        "unavailable",
        "available",
        "available",
    )
    assert fetch.call_count == 2
