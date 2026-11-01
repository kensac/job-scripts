"""Provider-reported spend, and what money is left, for each AI provider.

Spend is what the provider reports for completed UTC days. Money left is
never read from a provider, because neither reports a prepaid balance. It is
derived, and labelled so, from one of two figures:

- a prepaid grant an admin entered (`provider_prepaid_credits`), minus the
  provider-reported spend since the grant day;
- OpenAI's own hard spend limit, minus the provider-reported spend so far
  this UTC month.

Today's partial day is in neither, so each derived figure is as of the end of
yesterday, UTC.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from api import db
from core import provider_costs

PROVIDERS = ("openai", "anthropic")
# A month to date and the last 30 days both fit in 31 completed days.
_SPEND_DAYS = 31
# The costs endpoints read at most a year.
_MAX_DAYS = 365
# Warnings wait for the daily digest, so money that runs out sooner than a
# day would be gone before its warning was sent.
_CRITICAL_RUNWAY_DAYS = 1


class ProviderUnreadable(Exception):
    """A configured provider's billing could not be read whole."""


def _day(unix: int) -> datetime.date:
    return datetime.datetime.fromtimestamp(unix, datetime.UTC).date()


def read(provider: str, granted_on: str | None) -> dict[str, Any]:
    """The provider's raw billing: costs over a window that covers the month,
    the last 30 days and the grant day, and OpenAI's spend limit."""
    days = _SPEND_DAYS
    if granted_on:
        today = datetime.datetime.now(datetime.UTC).date()
        days = min(_MAX_DAYS, max(days, (today - datetime.date.fromisoformat(granted_on)).days))
    if provider == "openai":
        return {
            "costs": provider_costs.fetch_costs(days),
            "limit": provider_costs.fetch_openai_spend_limit(),
        }
    return {"costs": provider_costs.fetch_anthropic_costs(days), "limit": None}


def readable(raw: dict[str, Any]) -> bool:
    """Whether every configured part of a read succeeded. A read that is not
    must not be cached or judged as all clear."""
    parts = [raw["costs"], raw["limit"]] if raw["limit"] else [raw["costs"]]
    return all(part["status"] != "unavailable" for part in parts)


def _sum(daily: list[dict], since: datetime.date) -> Decimal:
    return sum(
        (b["total_usd"] for b in daily if _day(b["start_time"]) >= since),
        Decimal(0),
    )


def _unknown(basis: str, reason: str) -> dict[str, Any]:
    """A basis that applies but cannot be derived: shown as unknown, never 0."""
    return {"basis": basis, "amount_usd": None, "unavailable": reason, "alert": None}


def _headroom(
    *,
    basis: str,
    label: str,
    amount: Decimal,
    rate: Decimal,
    ends_on: datetime.date | None,
    ends_with_money_kept: bool,
    today: datetime.date,
    alert_usd: int,
    alert_days: int,
    detail: dict[str, Any],
) -> dict[str, Any]:
    """Money left on one basis, its runway at the recent rate, and whether
    that is an alert.

    `ends_on` is the day the figure stops applying: a prepaid grant's expiry
    (the money is lost) or the spend limit's monthly reset (the headroom
    returns). Only the first can run a provider dry."""
    runway = float(amount / rate) if rate > 0 and amount > 0 else None
    days_to_end = (ends_on - today).days if ends_on else None
    message = None
    if amount <= 0:
        message = f"{label} is exhausted (${float(amount):.2f})."
    elif amount <= alert_usd:
        message = f"{label} is ${float(amount):.2f}, at or under the ${alert_usd} alert floor."
    elif (
        runway is not None and runway < alert_days and (days_to_end is None or runway < days_to_end)
    ):
        message = (
            f"{label} is ${float(amount):.2f}: {runway:.1f} days at the last seven days' "
            f"${float(rate):.2f} a day, under the {alert_days}-day alert."
        )
    elif not ends_with_money_kept and days_to_end is not None and days_to_end < alert_days:
        message = (
            f"{label} is ${float(amount):.2f} and expires on {ends_on}, in {days_to_end} days; "
            "what is not spent by then is lost."
        )
    soonest = min(
        (d for d in (runway, None if ends_with_money_kept else days_to_end) if d is not None),
        default=None,
    )
    alert = None
    if message:
        critical = amount <= 0 or (soonest is not None and soonest < _CRITICAL_RUNWAY_DAYS)
        alert = {"severity": "critical" if critical else "warning", "message": message}
    return {
        "basis": basis,
        "label": label,
        "amount_usd": amount,
        "runway_days": runway,
        "ends_on": ends_on.isoformat() if ends_on else None,
        "alert": alert,
        "unavailable": None,
        **detail,
    }


def summarize(
    provider: str,
    raw: dict[str, Any],
    prepaid: dict[str, Any] | None,
    today: datetime.date,
    alert_usd: int,
    alert_days: int,
) -> dict[str, Any]:
    """The provider's spend and its derived headrooms. Pure: no I/O, so the
    alert rule is tested on recorded provider answers."""
    costs, limit = raw["costs"], raw["limit"]
    report: dict[str, Any] = {
        "provider": provider,
        "status": costs["status"],
        "reason": costs["reason"],
        "scope": costs["scope"],
        "fetched_at": costs["fetched_at"],
        "spend": None,
        "limit": limit,
        "headrooms": [],
    }
    if costs["status"] != "available":
        return report
    daily = costs["daily"]
    rate = _sum(daily, today - datetime.timedelta(days=7)) / 7
    last_30 = [b for b in daily if _day(b["start_time"]) >= today - datetime.timedelta(days=30)]
    by_item: dict[str, Decimal] = {}
    for bucket in last_30:
        for item in bucket["items"]:
            name = item["model"] or item["line_item"] or "unlabelled"
            by_item[name] = by_item.get(name, Decimal(0)) + item["amount_usd"]
    month_start = today.replace(day=1)
    month_to_date = _sum(daily, month_start)
    report["spend"] = {
        "yesterday_usd": _sum(daily, today - datetime.timedelta(days=1)),
        "last_7_days_usd": rate * 7,
        "last_30_days_usd": sum((b["total_usd"] for b in last_30), Decimal(0)),
        "month_to_date_usd": month_to_date,
        "daily_rate_usd": rate,
        # OpenAI's line items name a model and a token kind together; the
        # Anthropic report names the model on its own.
        "item_basis": "model" if provider == "anthropic" else "line_item",
        "by_item": [
            {"item": name, "amount_usd": amount}
            for name, amount in sorted(by_item.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "daily": [
            {"day": _day(b["start_time"]).isoformat(), "total_usd": b["total_usd"]} for b in last_30
        ],
    }
    if prepaid:
        granted_on = datetime.date.fromisoformat(prepaid["granted_on"])
        expires_on = (
            datetime.date.fromisoformat(prepaid["expires_on"])
            if prepaid.get("expires_on")
            else None
        )
        if _day(costs["start_time"]) > granted_on:
            report["headrooms"].append(_unknown("prepaid", "GRANT_OUTSIDE_WINDOW"))
        else:
            granted = Decimal(str(prepaid["amount_usd"]))
            spent = _sum(daily, granted_on)
            report["headrooms"].append(
                _headroom(
                    basis="prepaid",
                    label=f"{provider} prepaid credit (derived)",
                    amount=granted - spent,
                    rate=rate,
                    ends_on=expires_on,
                    ends_with_money_kept=False,
                    today=today,
                    alert_usd=alert_usd,
                    alert_days=alert_days,
                    detail={
                        "derivation": "configured_prepaid_minus_reported_spend",
                        "granted_usd": granted,
                        "granted_on": granted_on.isoformat(),
                        "spent_usd": spent,
                    },
                )
            )
    if limit and limit["status"] == "available" and limit["threshold_usd"] > 0:
        if costs["scope"] != "organization":
            # The limit is the organization's; project-scoped spend under it
            # would overstate the headroom.
            report["headrooms"].append(_unknown("spend_limit", "COSTS_PROJECT_SCOPED"))
        else:
            report["headrooms"].append(
                _headroom(
                    basis="spend_limit",
                    label=f"{provider} hard spend limit headroom (derived)",
                    amount=limit["threshold_usd"] - month_to_date,
                    rate=rate,
                    ends_on=(month_start + datetime.timedelta(days=32)).replace(day=1),
                    ends_with_money_kept=True,
                    today=today,
                    alert_usd=alert_usd,
                    alert_days=alert_days,
                    detail={
                        "derivation": "provider_limit_minus_reported_month_spend",
                        "limit_usd": limit["threshold_usd"],
                        "spent_usd": month_to_date,
                    },
                )
            )
    return report


def thresholds() -> tuple[int, int]:
    return int(db.get_config("provider_balance_alert_usd")), int(
        db.get_config("provider_runway_alert_days")
    )


def prepaid_for(provider: str) -> dict[str, Any] | None:
    return (db.get_config("provider_prepaid_credits") or {}).get(provider)


def report(provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    alert_usd, alert_days = thresholds()
    today = datetime.datetime.now(datetime.UTC).date()
    return summarize(provider, raw, prepaid_for(provider), today, alert_usd, alert_days)
