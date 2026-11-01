"""Read provider-reported billing without substituting local token estimates.

Neither provider exposes a remaining prepaid credit balance or a credit
grant's expiry through any documented endpoint (checked 2026-11-01). OpenAI
exposes the organization's hard spend limit; Anthropic lists spend limits only
in an early access preview. What is read here is what each provider reports.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from core.env import env_list

_COSTS_URL = "https://api.openai.com/v1/organization/costs"
_SPEND_LIMIT_URL = "https://api.openai.com/v1/organization/spend_limit"
_ANTHROPIC_COSTS_URL = "https://api.anthropic.com/v1/organizations/cost_report"
# The legacy cost report: no beta header, and data within about five minutes.
# The analytics cost endpoint that succeeds it is a beta that refreshes within
# four to twenty-four hours, too late for a runway alert.
_ANTHROPIC_VERSION = "2023-06-01"
# Each provider's documented maximum buckets per page: OpenAI's costs endpoint
# 180 daily buckets, Anthropic's cost report 31.
_PAGE_BUCKETS = 180
_ANTHROPIC_PAGE_BUCKETS = 31
# An interactive billing lookup must finish even when pagination stalls.
_REQUEST_BUDGET_SECONDS = 20
_DAY = 86400


class _Unavailable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _integer(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("invalid timestamp")
    return value


def _nullable_text(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("invalid dimension")
    return value


def _checked_bounds(lower: int, upper: int, start: int, end: int) -> None:
    if lower < start or upper > end or upper - lower != _DAY or lower % _DAY:
        raise ValueError("invalid bucket bounds")


def _results(raw: Any) -> list:
    if not isinstance(raw, dict):
        raise ValueError("invalid bucket")
    results = raw.get("results")
    if not isinstance(results, list):
        raise ValueError("invalid results")
    return results


def _day_bucket(lower: int, upper: int, items: list[dict]) -> dict:
    return {
        "start_time": lower,
        "end_time": upper,
        "total_usd": sum((item["amount_usd"] for item in items), Decimal(0)),
        "items": items,
    }


def _openai_bucket(raw: Any, start: int, end: int, projects: list[str]) -> dict:
    if not isinstance(raw, dict) or raw.get("object") != "bucket":
        raise ValueError("invalid bucket")
    lower, upper = _integer(raw.get("start_time")), _integer(raw.get("end_time"))
    _checked_bounds(lower, upper, start, end)
    items = []
    for result in _results(raw):
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
                "model": None,
            }
        )
    return _day_bucket(lower, upper, items)


def _rfc3339(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() != timedelta(0):
        raise ValueError("timestamp not in UTC")
    return int(moment.timestamp())


def _anthropic_bucket(raw: Any, start: int, end: int, projects: list[str]) -> dict:
    lower = _rfc3339(raw.get("starting_at") if isinstance(raw, dict) else None)
    upper = _rfc3339(raw.get("ending_at"))
    _checked_bounds(lower, upper, start, end)
    items = []
    for result in _results(raw):
        if not isinstance(result, dict) or result.get("currency") != "USD":
            raise ValueError("missing dollar amount")
        amount = result.get("amount")
        if not isinstance(amount, str):
            raise ValueError("invalid amount")
        # Decimal text in cents: "123.45" is $1.2345.
        cents = Decimal(amount)
        if not cents.is_finite():
            raise ValueError("invalid amount")
        items.append(
            {
                "amount_usd": cents / 100,
                # A workspace is Anthropic's spend scope, as a project is OpenAI's.
                "project_id": _nullable_text(result.get("workspace_id")),
                "line_item": _nullable_text(result.get("description")),
                "model": _nullable_text(result.get("model")),
            }
        )
    return _day_bucket(lower, upper, items)


def _paginate(
    url: str,
    headers: dict[str, str],
    params: dict[str, Any],
    days: int,
    bucket: Callable[[Any, int, int, list[str]], dict],
    window: tuple[int, int],
    projects: list[str],
    page_object: str | None,
) -> list[dict]:
    """Every daily bucket of the window, or _Unavailable: a partial read is
    never returned as a total."""
    daily, seen_days, cursors = [], set(), set()
    deadline = time.monotonic() + _REQUEST_BUDGET_SECONDS
    try:
        # At least one distinct daily bucket must advance each nonterminal
        # page, so days bounds the page count independently of provider cursors.
        for _ in range(days):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Unavailable("REQUEST_TIMEOUT")
            with requests.get(
                url,
                headers=headers,
                params=params,
                timeout=(min(3, remaining), min(8, remaining)),
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise _Unavailable("REQUEST_FAILED")
                page = json.loads(response.text, parse_float=Decimal)
            if not isinstance(page, dict) or (page_object and page.get("object") != page_object):
                raise ValueError("invalid page")
            data, more = page.get("data"), page.get("has_more")
            if not isinstance(data, list) or type(more) is not bool:
                raise ValueError("invalid pagination")
            for raw in data:
                parsed = bucket(raw, *window, projects)
                if parsed["start_time"] in seen_days:
                    raise ValueError("duplicate bucket")
                seen_days.add(parsed["start_time"])
                daily.append(parsed)
            if not more:
                return sorted(daily, key=lambda b: b["start_time"])
            cursor = page.get("next_page")
            if not data or not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("invalid pagination cursor")
            cursors.add(cursor)
            params["page"] = cursor
    except requests.RequestException:
        raise _Unavailable("REQUEST_FAILED") from None
    except ValueError, TypeError, InvalidOperation:
        raise _Unavailable("INVALID_RESPONSE") from None
    raise _Unavailable("PAGINATION_LIMIT")


def _fetch(provider: str, days: int, key: str) -> dict:
    """Latest completed UTC days, as reported now, not a finalized invoice.

    Failed or incomplete pagination returns no total. Credentials and upstream
    error bodies never enter the response; no regular inference key is used.
    """
    now = datetime.now(UTC)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    projects = sorted(set(env_list("OPENAI_BILLING_PROJECT_IDS"))) if provider == "openai" else []
    report = {
        "provider": provider,
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
    begin = end - timedelta(days=days)
    start = int(begin.timestamp())
    report["start_time"] = start
    key = key.strip()
    if not key:
        return {**report, "status": "not_configured", "reason": "NOT_CONFIGURED"}
    window = (start, report["end_time"])
    if provider == "openai":
        params: dict[str, Any] = {
            "start_time": start,
            "end_time": report["end_time"],
            "bucket_width": "1d",
            "limit": _PAGE_BUCKETS,
            "group_by[]": ["project_id", "line_item"],
        }
        if projects:
            params["project_ids[]"] = projects
        source = (_COSTS_URL, {"Authorization": f"Bearer {key}"}, _openai_bucket, "page")
    else:
        params = {
            "starting_at": begin.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ending_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bucket_width": "1d",
            "limit": _ANTHROPIC_PAGE_BUCKETS,
            "group_by[]": ["description"],
        }
        headers = {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION}
        source = (_ANTHROPIC_COSTS_URL, headers, _anthropic_bucket, None)
    url, headers, bucket, page_object = source
    try:
        daily = _paginate(url, headers, params, days, bucket, window, projects, page_object)
    except _Unavailable as unavailable:
        return {**report, "reason": unavailable.reason}
    return {
        **report,
        "status": "available",
        "total_usd": sum((b["total_usd"] for b in daily), Decimal(0)),
        "daily": daily,
    }


def fetch_costs(days: int) -> dict:
    """OpenAI's organization costs, scoped by OPENAI_BILLING_PROJECT_IDS."""
    return _fetch("openai", days, os.environ.get("OPENAI_ADMIN_KEY", ""))


def fetch_anthropic_costs(days: int) -> dict:
    """Anthropic's organization cost report, grouped by description so each
    token cost names its model. Priority Tier costs are not in this report."""
    return _fetch("anthropic", days, os.environ.get("ANTHROPIC_ADMIN_KEY", ""))


def fetch_openai_spend_limit() -> dict:
    """The organization's monthly hard spend limit as OpenAI reports it.

    `enforcement` is the provider's own word, passed through: the reference
    says only "whether the hard spend limit is currently enforcing". A
    threshold of 0 is how the endpoint answers when no limit is set.
    """
    report: dict[str, Any] = {
        "status": "unavailable",
        "reason": None,
        "threshold_usd": None,
        "interval": None,
        "enforcement": None,
    }
    key = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    if not key:
        return {**report, "status": "not_configured", "reason": "NOT_CONFIGURED"}
    try:
        with requests.get(
            _SPEND_LIMIT_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=(3, 8),
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                return {**report, "reason": "REQUEST_FAILED"}
            body = json.loads(response.text)
        if not isinstance(body, dict) or body.get("object") != "organization.spend_limit":
            raise ValueError("invalid spend limit")
        cents = body.get("threshold_amount")
        enforcement = body.get("enforcement")
        interval = body.get("interval")
        if type(cents) is not int or cents < 0 or body.get("currency") != "USD":
            raise ValueError("invalid threshold")
        if not isinstance(enforcement, dict) or not isinstance(enforcement.get("status"), str):
            raise ValueError("invalid enforcement")
        if not isinstance(interval, str):
            raise ValueError("invalid interval")
    except requests.RequestException:
        return {**report, "reason": "REQUEST_FAILED"}
    except ValueError, TypeError:
        return {**report, "reason": "INVALID_RESPONSE"}
    return {
        **report,
        "status": "available",
        "threshold_usd": Decimal(cents) / 100,
        "interval": interval,
        "enforcement": enforcement["status"],
    }
