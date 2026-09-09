"""Read provider-reported billing without substituting local token estimates."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

_COSTS_URL = "https://api.openai.com/v1/organization/costs"
# The API caps each page at 180 daily buckets; a year needs pagination.
_PAGE_BUCKETS = 180
# An interactive billing lookup must finish even when pagination stalls.
_REQUEST_BUDGET_SECONDS = 20


def _integer(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("invalid timestamp")
    return value


def _nullable_text(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("invalid dimension")
    return value


def _bucket(raw: Any, start: int, end: int, projects: list[str]) -> dict:
    if not isinstance(raw, dict) or raw.get("object") != "bucket":
        raise ValueError("invalid bucket")
    lower, upper = _integer(raw.get("start_time")), _integer(raw.get("end_time"))
    if lower < start or upper > end or upper - lower != 86400 or lower % 86400:
        raise ValueError("invalid bucket bounds")
    results = raw.get("results")
    if not isinstance(results, list):
        raise ValueError("invalid results")
    items = []
    for result in results:
        if not isinstance(result, dict) or result.get("object") != "organization.costs.result":
            raise ValueError("invalid cost result")
        amount = result.get("amount")
        if not isinstance(amount, dict) or amount.get("currency") != "usd":
            raise ValueError("missing dollar amount")
        value = amount.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ValueError("invalid amount")
        dollars = Decimal(value)
        if not dollars.is_finite():
            raise ValueError("invalid amount")
        project = _nullable_text(result.get("project_id"))
        if projects and project not in projects:
            raise ValueError("unexpected project scope")
        items.append(
            {
                "amount_usd": dollars,
                "project_id": project,
                "line_item": _nullable_text(result.get("line_item")),
            }
        )
    return {
        "start_time": lower,
        "end_time": upper,
        "total_usd": sum((item["amount_usd"] for item in items), Decimal(0)),
        "items": items,
    }


def fetch_costs(days: int) -> dict:
    """Latest completed UTC days, as reported now, not a finalized invoice.

    Failed or incomplete pagination returns no total. Credentials and upstream
    error bodies never enter the response; no regular inference key is used.
    """
    now = datetime.now(UTC)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    projects = sorted(
        {
            p.strip()
            for p in os.environ.get("OPENAI_BILLING_PROJECT_IDS", "").split(",")
            if p.strip()
        }
    )
    report = {
        "provider": "openai",
        "basis": "provider_reported_cost",
        "status": "unavailable",
        "reason": None,
        "scope": "projects" if projects else "organization",
        "project_ids": projects,
        "window_basis": "completed_utc_days",
        "start_time": None,
        "end_time": int(end.timestamp()),
        "fetched_at": now.isoformat(),
        "total_usd": None,
        "daily": [],
    }
    if type(days) is not int or not 1 <= days <= 365:
        return {**report, "reason": "INVALID_WINDOW"}
    start = int((end - timedelta(days=days)).timestamp())
    report["start_time"] = start
    key = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    if not key:
        return {**report, "status": "not_configured", "reason": "NOT_CONFIGURED"}
    params: dict[str, Any] = {
        "start_time": start,
        "end_time": report["end_time"],
        "bucket_width": "1d",
        "limit": _PAGE_BUCKETS,
        "group_by[]": ["project_id", "line_item"],
    }
    if projects:
        params["project_ids[]"] = projects
    daily, seen_days, cursors = [], set(), set()
    deadline = time.monotonic() + _REQUEST_BUDGET_SECONDS
    try:
        # At least one distinct daily bucket must advance each nonterminal
        # page, so days bounds the page count independently of provider cursors.
        for _ in range(days):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**report, "reason": "REQUEST_TIMEOUT"}
            with requests.get(
                _COSTS_URL,
                headers={"Authorization": f"Bearer {key}"},
                params=params,
                timeout=(min(3, remaining), min(8, remaining)),
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    return {**report, "reason": "REQUEST_FAILED"}
                page = json.loads(response.text, parse_float=Decimal)
            if not isinstance(page, dict) or page.get("object") != "page":
                raise ValueError("invalid page")
            data, more = page.get("data"), page.get("has_more")
            if not isinstance(data, list) or type(more) is not bool:
                raise ValueError("invalid pagination")
            for raw in data:
                bucket = _bucket(raw, start, int(end.timestamp()), projects)
                if bucket["start_time"] in seen_days:
                    raise ValueError("duplicate bucket")
                seen_days.add(bucket["start_time"])
                daily.append(bucket)
            if not more:
                return {
                    **report,
                    "status": "available",
                    "total_usd": sum((b["total_usd"] for b in daily), Decimal(0)),
                    "daily": sorted(daily, key=lambda b: b["start_time"]),
                }
            cursor = page.get("next_page")
            if not data or not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("invalid pagination cursor")
            cursors.add(cursor)
            params["page"] = cursor
    except requests.RequestException:
        return {**report, "reason": "REQUEST_FAILED"}
    except (ValueError, TypeError, InvalidOperation):
        return {**report, "reason": "INVALID_RESPONSE"}
    return {**report, "reason": "PAGINATION_LIMIT"}
