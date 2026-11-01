"""The derived balance and its alert rule, on parsed provider answers."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from api.spend.balance import summarize

TODAY = date(2026, 11, 12)


def _costs(per_day, days=31, scope="organization", today=TODAY):
    end = datetime(today.year, today.month, today.day, tzinfo=UTC)
    start = end - timedelta(days=days)
    daily = []
    for n in range(days):
        lower = int((start + timedelta(days=n)).timestamp())
        amount = Decimal(str(per_day))
        items = [{"amount_usd": amount, "project_id": None, "line_item": "x", "model": "m"}]
        daily.append(
            {"start_time": lower, "end_time": lower + 86400, "total_usd": amount, "items": items}
        )
    return {
        "status": "available",
        "reason": None,
        "scope": scope,
        "fetched_at": end.isoformat(),
        "start_time": int(start.timestamp()),
        "daily": daily,
    }


def _limit(dollars):
    return {
        "status": "available",
        "reason": None,
        "threshold_usd": Decimal(dollars),
        "interval": "month",
        "enforcement": "enforcing",
    }


def _only(report):
    (headroom,) = report["headrooms"]
    return headroom


def _prepaid(amount, granted_on="2026-11-01", expires_on=None, per_day=1, today=TODAY):
    raw = {"costs": _costs(per_day, today=today), "limit": None}
    prepaid = {"amount_usd": amount, "granted_on": granted_on, "expires_on": expires_on}
    return _only(summarize("anthropic", raw, prepaid, today, 5, 7))


def test_spend_windows_and_model_breakdown():
    report = summarize("anthropic", {"costs": _costs(2), "limit": None}, None, TODAY, 5, 7)
    spend = report["spend"]
    assert spend["yesterday_usd"] == 2 and spend["last_7_days_usd"] == 14
    assert spend["last_30_days_usd"] == 60 and spend["month_to_date_usd"] == 22
    assert spend["by_item"] == [{"item": "m", "amount_usd": Decimal(60)}]
    assert report["headrooms"] == []


def test_prepaid_credit_is_grant_minus_reported_spend_since_grant_day():
    headroom = _prepaid(200, expires_on="2026-12-31")
    # Eleven completed days from Nov 1 to Nov 11 at $1.
    assert headroom["amount_usd"] == 189 and headroom["spent_usd"] == 11
    assert headroom["derivation"] == "configured_prepaid_minus_reported_spend"
    assert headroom["runway_days"] == 189 and headroom["alert"] is None


def test_credit_expiring_inside_the_runway_alert_warns_about_lost_money():
    headroom = _prepaid(200, expires_on="2026-11-17")
    assert headroom["alert"]["severity"] == "warning"
    assert "expires on 2026-11-17" in headroom["alert"]["message"]


@pytest.mark.parametrize(
    ("amount", "per_day", "severity"),
    [
        (53, 3, "warning"),  # $20 left, 6.7 days at $3: under seven
        (62, 3, None),  # $29 left, 9.7 days
        (16, 1, "warning"),  # $5 left: at the floor
        (11, 1, "critical"),  # $0 left: exhausted
        (44, 3.9, "critical"),  # $1.10 left above a $0 floor, 0.3 days: before a digest
    ],
)
def test_prepaid_alert_floor_and_runway(amount, per_day, severity):
    raw = {"costs": _costs(per_day), "limit": None}
    prepaid = {"amount_usd": amount, "granted_on": "2026-11-01"}
    floor = 0 if severity == "critical" and amount == 44 else 5
    headroom = _only(summarize("anthropic", raw, prepaid, TODAY, floor, 7))
    assert (headroom["alert"] or {}).get("severity") == severity


def test_spend_limit_headroom_alerts_only_when_it_runs_out_before_the_reset():
    raw = {"costs": _costs(15), "limit": _limit(250)}
    # $85 left after $165 this month: 5.7 days, and the reset is 19 days off.
    headroom = _only(summarize("openai", raw, None, TODAY, 5, 7))
    assert headroom["amount_usd"] == 85 and headroom["ends_on"] == "2026-12-01"
    assert headroom["derivation"] == "provider_limit_minus_reported_month_spend"
    assert headroom["alert"]["severity"] == "warning"

    late = date(2026, 11, 29)
    raw = {"costs": _costs(15, today=late), "limit": _limit(450)}
    # $30 left, two days, and the limit resets in two: no outage.
    assert _only(summarize("openai", raw, None, late, 5, 7))["alert"] is None


def test_no_limit_set_and_project_scope_claim_no_headroom():
    raw = {"costs": _costs(1), "limit": _limit(0)}
    assert summarize("openai", raw, None, TODAY, 5, 7)["headrooms"] == []
    raw = {"costs": _costs(1, scope="projects"), "limit": _limit(100)}
    headroom = _only(summarize("openai", raw, None, TODAY, 5, 7))
    assert headroom["amount_usd"] is None and headroom["unavailable"] == "COSTS_PROJECT_SCOPED"


def test_grant_before_the_read_window_is_unknown_not_a_balance():
    headroom = _prepaid(200, granted_on="2025-01-01")
    assert headroom["amount_usd"] is None and headroom["unavailable"] == "GRANT_OUTSIDE_WINDOW"


def test_unavailable_costs_show_no_spend_and_no_balance():
    costs = {**_costs(1), "status": "unavailable", "reason": "REQUEST_FAILED", "daily": []}
    prepaid = {"amount_usd": 200, "granted_on": "2026-11-01"}
    report = summarize("anthropic", {"costs": costs, "limit": None}, prepaid, TODAY, 5, 7)
    assert report["spend"] is None and report["headrooms"] == []
