import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import requests

from core import provider_costs


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 9, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "test-admin-key")
    monkeypatch.delenv("OPENAI_BILLING_PROJECT_IDS", raising=False)
    monkeypatch.setattr(provider_costs, "datetime", _Clock)


def _bucket(day, value="0.06", project=None):
    start = int(datetime(2026, 9, day, tzinfo=UTC).timestamp())
    return {
        "object": "bucket",
        "start_time": start,
        "end_time": start + 86400,
        "results": [
            {
                "object": "organization.costs.result",
                "amount": {"currency": "usd", "value": value},
                "project_id": project,
                "line_item": "model input",
            }
        ],
    }


def _page(*buckets, next_page=None):
    return {
        "object": "page",
        "data": list(buckets),
        "has_more": next_page is not None,
        "next_page": next_page,
    }


def _serve(monkeypatch, pages):
    calls = []

    def get(url, **kwargs):
        calls.append((url, {**kwargs, "params": dict(kwargs.get("params") or {})}))
        value = pages[len(calls) - 1]
        if isinstance(value, Exception):
            raise value
        response = requests.Response()
        response.status_code = value if isinstance(value, int) else 200
        # Keep amount text exact, as the provider's JSON numeric field is.
        body = (
            json.dumps(value)
            .replace('"0.06"', "0.06")
            .replace('"0.1234567890123456789"', "0.1234567890123456789")
            .replace('"-0.01"', "-0.01")
        )
        response._content = body.encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(provider_costs.requests, "get", get)
    return calls


def test_paginated_project_costs_preserve_decimal_credits_and_utc_window(monkeypatch):
    monkeypatch.setenv("OPENAI_BILLING_PROJECT_IDS", " proj-a,proj-a ")
    calls = _serve(
        monkeypatch,
        [
            _page(_bucket(7, "0.1234567890123456789", "proj-a"), next_page="page-two"),
            _page(_bucket(8, "-0.01", "proj-a")),
        ],
    )
    result = provider_costs.fetch_costs(2)
    assert result["status"] == "available"
    assert result["scope"] == "projects" and result["project_ids"] == ["proj-a"]
    assert result["total_usd"] == Decimal("0.1134567890123456789")
    assert len(result["daily"]) == 2
    assert result["start_time"] == int(datetime(2026, 9, 7, tzinfo=UTC).timestamp())
    assert result["end_time"] == int(datetime(2026, 9, 9, tzinfo=UTC).timestamp())
    assert calls[1][1]["params"]["page"] == "page-two"
    assert calls[0][1]["params"]["project_ids[]"] == ["proj-a"]
    assert calls[0][1]["params"]["group_by[]"] == ["project_id", "line_item"]
    assert calls[0][1]["allow_redirects"] is False
    assert "test-admin-key" not in str(result)


def test_missing_admin_key_never_uses_regular_key_or_reports_zero(monkeypatch):
    monkeypatch.delenv("OPENAI_ADMIN_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "ordinary-key")
    calls = _serve(monkeypatch, [])
    result = provider_costs.fetch_costs(7)
    assert result["status"] == "not_configured" and result["total_usd"] is None
    assert result["scope"] == "organization" and not calls


@pytest.mark.parametrize("failure", [401, 429, 302, requests.Timeout("secret upstream body")])
def test_request_failure_discards_partial_totals_and_redacts_errors(monkeypatch, failure):
    _serve(monkeypatch, [_page(_bucket(7), next_page="next"), failure])
    result = provider_costs.fetch_costs(2)
    assert result["status"] == "unavailable" and result["total_usd"] is None
    assert result["daily"] == []
    assert "secret" not in str(result) and "test-admin-key" not in str(result)


@pytest.mark.parametrize(
    "defect",
    ["missing_amount", "currency", "boolean_amount", "duplicate", "cursor", "scope", "range"],
)
def test_invalid_provider_shape_never_becomes_billing_total(monkeypatch, defect):
    bucket = _bucket(8)
    page = _page(bucket)
    if defect == "missing_amount":
        del bucket["results"][0]["amount"]
    elif defect == "currency":
        bucket["results"][0]["amount"]["currency"] = "eur"
    elif defect == "boolean_amount":
        bucket["results"][0]["amount"]["value"] = True
    elif defect == "duplicate":
        page["data"].append(bucket)
    elif defect == "cursor":
        page["has_more"] = True
    elif defect == "scope":
        monkeypatch.setenv("OPENAI_BILLING_PROJECT_IDS", "proj-a")
    elif defect == "range":
        bucket["end_time"] += 1
    _serve(monkeypatch, [page])
    result = provider_costs.fetch_costs(2)
    assert result["status"] == "unavailable" and result["reason"] == "INVALID_RESPONSE"
    assert result["total_usd"] is None and result["daily"] == []


def test_empty_complete_response_is_observed_zero_and_year_window_uses_pages(monkeypatch):
    calls = _serve(monkeypatch, [_page()])
    result = provider_costs.fetch_costs(365)
    assert result["status"] == "available" and result["total_usd"] == Decimal(0)
    assert calls[0][1]["params"]["limit"] == 180


def test_incomplete_last_page_is_not_silently_truncated(monkeypatch):
    _serve(monkeypatch, [_page(_bucket(8), next_page="more")])
    result = provider_costs.fetch_costs(1)
    assert result["reason"] == "PAGINATION_LIMIT" and result["total_usd"] is None


@pytest.mark.parametrize("days", [0, 366, True, 1.5])
def test_invalid_window_never_requests_billing(monkeypatch, days):
    calls = _serve(monkeypatch, [])
    result = provider_costs.fetch_costs(days)
    assert result["reason"] == "INVALID_WINDOW" and result["total_usd"] is None
    assert not calls


def test_repeated_cursor_discards_all_partial_results(monkeypatch):
    calls = _serve(
        monkeypatch,
        [
            _page(_bucket(7), next_page="repeated"),
            _page(_bucket(8), next_page="repeated"),
        ],
    )
    result = provider_costs.fetch_costs(3)
    assert result["reason"] == "INVALID_RESPONSE" and result["total_usd"] is None
    assert len(calls) == 2


# Anthropic's cost report as its reference documents it (fetched 2026-11-01,
# platform.claude.com/docs/en/api/beta/organization/cost_report/retrieve):
# amounts are decimal text in cents, buckets are RFC 3339 days.
def _anthropic_bucket(day, results):
    return {
        "starting_at": f"2026-09-{day:02d}T00:00:00Z",
        "ending_at": f"2026-09-{day + 1:02d}T00:00:00Z",
        "results": results,
    }


_ANTHROPIC_RESULT = {
    "amount": "123.78912",
    "context_window": "0-200k",
    "cost_type": "tokens",
    "currency": "USD",
    "description": "Claude Opus 5.5 Usage - Input Tokens",
    "inference_geo": "global",
    "model": "claude-opus-5-5",
    "service_tier": "standard",
    "speed": "standard",
    "token_type": "cache_creation.ephemeral_1h_input_tokens",
    "workspace_id": "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ",
}


@pytest.fixture
def anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "test-anthropic-admin")


def test_anthropic_cost_report_reads_cents_by_model_over_pages(monkeypatch, anthropic):
    web_search = {**_ANTHROPIC_RESULT, "amount": "1", "cost_type": "web_search", "model": None}
    calls = _serve(
        monkeypatch,
        [
            {
                "data": [_anthropic_bucket(7, [_ANTHROPIC_RESULT, web_search])],
                "has_more": True,
                "next_page": "page_MjAyNS0wNS0xNFQwMDowMDowMFo=",
            },
            {"data": [_anthropic_bucket(8, [])], "has_more": False, "next_page": None},
        ],
    )
    result = provider_costs.fetch_anthropic_costs(2)
    assert result["status"] == "available" and result["provider"] == "anthropic"
    assert result["total_usd"] == Decimal("1.2478912")
    first = result["daily"][0]["items"][0]
    assert first["amount_usd"] == Decimal("1.2378912") and first["model"] == "claude-opus-5-5"
    assert first["line_item"] == "Claude Opus 5.5 Usage - Input Tokens"
    assert result["daily"][1]["total_usd"] == 0
    params = calls[0][1]["params"]
    assert params["starting_at"] == "2026-09-07T00:00:00Z"
    assert params["ending_at"] == "2026-09-09T00:00:00Z"
    assert params["group_by[]"] == ["description"] and params["limit"] == 31
    assert calls[0][1]["headers"]["x-api-key"] == "test-anthropic-admin"
    assert calls[1][1]["params"]["page"] == "page_MjAyNS0wNS0xNFQwMDowMDowMFo="
    assert "test-anthropic-admin" not in str(result)


def test_anthropic_without_its_admin_key_never_asks(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ordinary-key")
    calls = _serve(monkeypatch, [])
    result = provider_costs.fetch_anthropic_costs(7)
    assert result["status"] == "not_configured" and result["total_usd"] is None
    assert not calls


@pytest.mark.parametrize("defect", ["number_amount", "currency", "offset", "two_days", "outside"])
def test_anthropic_invalid_shape_never_becomes_a_total(monkeypatch, anthropic, defect):
    bucket = _anthropic_bucket(8, [dict(_ANTHROPIC_RESULT)])
    if defect == "number_amount":
        bucket["results"][0]["amount"] = 12
    elif defect == "currency":
        bucket["results"][0]["currency"] = "EUR"
    elif defect == "offset":
        bucket["starting_at"] = "2026-09-08T00:00:00+02:00"
    elif defect == "two_days":
        bucket["ending_at"] = "2026-09-10T00:00:00Z"
    elif defect == "outside":
        bucket = _anthropic_bucket(9, [])
    _serve(monkeypatch, [{"data": [bucket], "has_more": False, "next_page": None}])
    result = provider_costs.fetch_anthropic_costs(2)
    assert result["reason"] == "INVALID_RESPONSE" and result["total_usd"] is None


# OpenAI's spend limit as its reference documents it (fetched 2026-11-01,
# developers.openai.com/api/reference/resources/admin/subresources/organization/
# subresources/spend_limit): cents, and the provider's enforcement word.
_SPEND_LIMIT = {
    "object": "organization.spend_limit",
    "threshold_amount": 10000,
    "currency": "USD",
    "interval": "month",
    "enforcement": {"status": "enforcing"},
}


def test_openai_spend_limit_reads_dollars_and_enforcement(monkeypatch):
    calls = _serve(monkeypatch, [_SPEND_LIMIT])
    result = provider_costs.fetch_openai_spend_limit()
    assert result == {
        "status": "available",
        "reason": None,
        "threshold_usd": Decimal(100),
        "interval": "month",
        "enforcement": "enforcing",
    }
    assert calls[0][0].endswith("/v1/organization/spend_limit")


@pytest.mark.parametrize("defect", ["object", "float", "currency", "enforcement", 403])
def test_openai_spend_limit_failure_is_unavailable_not_zero(monkeypatch, defect):
    body = dict(_SPEND_LIMIT)
    if defect == "object":
        body["object"] = "organization.spend_limit.deleted"
    elif defect == "float":
        body["threshold_amount"] = 100.5
    elif defect == "currency":
        body["currency"] = "EUR"
    elif defect == "enforcement":
        body["enforcement"] = "enforcing"
    _serve(monkeypatch, [defect if isinstance(defect, int) else body])
    result = provider_costs.fetch_openai_spend_limit()
    assert result["status"] == "unavailable" and result["threshold_usd"] is None
