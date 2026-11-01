"""Provider money running out: a derived balance or spend-limit headroom."""

from __future__ import annotations

from typing import Any

from api.spend import balance


def detect_provider_money(provider: str) -> list[dict[str, Any]]:
    """`<provider>_balance_low` for each derived headroom under the admin's
    floor or runway. Unconfigured is silence; a configured provider whose read
    failed raises, so the section's open alerts stay open rather than reading
    as all clear. Read fresh each run, never from the page's cache."""
    raw = balance.read(provider, (balance.prepaid_for(provider) or {}).get("granted_on"))
    if not balance.readable(raw):
        parts = [raw["costs"]] + ([raw["limit"]] if raw["limit"] else [])
        reasons = ", ".join(p["reason"] for p in parts if p["status"] == "unavailable")
        raise balance.ProviderUnreadable(f"{provider} billing unreadable: {reasons}")
    return [
        {
            "kind": f"{provider}_balance_low",
            "subject": headroom["basis"],
            "severity": headroom["alert"]["severity"],
            "message": headroom["alert"]["message"],
            "detail": {
                "provider": provider,
                "basis": headroom["basis"],
                "derivation": headroom["derivation"],
                "amount_usd": float(headroom["amount_usd"]),
                "runway_days": headroom["runway_days"],
                "ends_on": headroom["ends_on"],
            },
        }
        for headroom in balance.report(provider, raw)["headrooms"]
        if headroom["alert"]
    ]
