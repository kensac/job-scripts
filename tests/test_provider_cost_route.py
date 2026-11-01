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


def test_balances_require_admin(client, user_headers):
    with patch("api.spend.balance.read") as read:
        response = client.get("/v1/admin/spend/balance", headers=user_headers)
    assert response.status_code == 403
    read.assert_not_called()


def test_balances_without_admin_keys_claim_nothing(client, admin_headers, monkeypatch):
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    body = client.get("/v1/admin/spend/balance", headers=admin_headers).json()
    assert [p["provider"] for p in body["providers"]] == ["openai", "anthropic"]
    for provider in body["providers"]:
        assert provider["status"] == "not_configured"
        assert provider["spend"] is None and provider["headrooms"] == []
    assert (body["alert_usd"], body["alert_days"]) == (5, 7)


def _raw(status, amount=1, limit=None):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=31)
    daily = []
    if status == "available":
        for n in range(31):
            lower = int((start + timedelta(days=n)).timestamp())
            item = {
                "amount_usd": Decimal(amount),
                "project_id": None,
                "line_item": "x",
                "model": None,
            }
            daily.append(
                {
                    "start_time": lower,
                    "end_time": lower + 86400,
                    "total_usd": Decimal(amount),
                    "items": [item],
                }
            )
    return {
        "costs": {
            **_report(status),
            "start_time": int(start.timestamp()),
            "end_time": int(end.timestamp()),
            "daily": daily,
        },
        "limit": limit,
    }


def test_balance_reads_are_cached_but_a_failure_is_not(client, admin_headers):
    with patch("api.spend.balance.read") as read:
        read.side_effect = lambda provider, granted_on: _raw("unavailable")
        first = client.get("/v1/admin/spend/balance", headers=admin_headers).json()
        read.side_effect = lambda provider, granted_on: _raw("available")
        second = client.get("/v1/admin/spend/balance", headers=admin_headers).json()
        third = client.get("/v1/admin/spend/balance", headers=admin_headers).json()
    assert [p["status"] for p in first["providers"]] == ["unavailable", "unavailable"]
    assert first["providers"][0]["spend"] is None
    assert [p["status"] for p in second["providers"]] == ["available", "available"]
    assert third == second
    assert read.call_count == 4


def test_prepaid_balance_is_labelled_derived(client, admin_headers, set_config):
    from datetime import UTC, datetime, timedelta

    granted = (datetime.now(UTC) - timedelta(days=10)).date().isoformat()
    set_config(
        "provider_prepaid_credits", {"anthropic": {"amount_usd": 200, "granted_on": granted}}
    )
    with patch("api.spend.balance.read", side_effect=lambda p, g: _raw("available")):
        body = client.get("/v1/admin/spend/balance", headers=admin_headers).json()
    (headroom,) = body["providers"][1]["headrooms"]
    assert headroom["derivation"] == "configured_prepaid_minus_reported_spend"
    assert "derived" in headroom["label"]
    assert headroom["amount_usd"] == 190 and headroom["spent_usd"] == 10


def test_health_alerts_on_spend_limit_headroom(monkeypatch):
    from decimal import Decimal

    from api import health

    limit = {
        "status": "available",
        "reason": None,
        "threshold_usd": Decimal(1),
        "interval": "month",
        "enforcement": "enforcing",
    }
    monkeypatch.setattr(
        "api.spend.balance.read",
        lambda provider, granted_on: _raw("available", 2, limit if provider == "openai" else None),
    )
    found = [f for f in health.detect() if f["kind"].endswith("_balance_low")]
    assert [(f["kind"], f["subject"]) for f in found] == [("openai_balance_low", "spend_limit")]


def test_unreadable_billing_is_a_detector_failure_not_all_clear(monkeypatch):
    from api import health

    monkeypatch.setattr(
        "api.spend.balance.read",
        lambda provider, granted_on: _raw(
            "unavailable" if provider == "anthropic" else "available"
        ),
    )
    run = health.detect()
    assert run.failed_detectors == ["_detect_anthropic_money"]
