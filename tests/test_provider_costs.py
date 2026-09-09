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
        calls.append((url, {**kwargs, "params": dict(kwargs["params"])}))
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
