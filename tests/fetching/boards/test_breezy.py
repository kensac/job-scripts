"""Breezy HR board behavior over JND's /json as served on 2026-11-01."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse

_URL = "https://jnd-inc.breezy.hr/json"

_ROW = {
    "id": "715190956730",
    "friendly_id": "715190956730-lte-5g-mobile-device-field-test-engineer-cell-phone-field-"
    "testing-261001-10",
    "name": "LTE/5G Mobile Device Field Test Engineer - Cell Phone Field Testing (261001-10)",
    "url": "https://jnd-inc.breezy.hr/p/715190956730-lte-5g-mobile-device-field-test-engineer-"
    "cell-phone-field-testing-261001-10",
    "published_date": "2026-10-27T20:33:30.621Z",
    "type": {"id": "fullTime", "name": "Full-Time"},
    "location": {
        "country": {"name": "United States", "id": "US"},
        "state": {"id": "TX", "name": "Texas"},
        "city": "Plano",
        "primary": True,
        "is_remote": False,
        "name": "Plano, TX",
        "id": "51bcef601978",
    },
    "department": None,
    "salary": "$33 \u2013 $34 / hour",
    "company": {"name": "JND", "logo_url": None, "friendly_id": "jnd-inc"},
    "locations": [
        {"city": "Plano", "name": "Plano, TX", "primary": True},
        {"city": "Austin", "name": "Austin, TX", "primary": False},
    ],
}


def _serve(monkeypatch, response):
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: asked.append(url) or response)
    return asked


def test_breezy_reads_every_posting_in_one_call(monkeypatch):
    asked = _serve(monkeypatch, BoardResponse([_ROW]))
    (p,) = boards.fetch_listings(_URL, "JND")
    assert asked == [_URL]
    assert boards.kind(_URL) == "breezy"
    assert p.url == _ROW["url"]
    assert p.locations == ["Plano, TX", "Austin, TX"]
    assert p.company == "JND"
    assert p.date_posted == int(
        datetime.datetime(2026, 10, 27, 20, 33, 30, 621000, datetime.UTC).timestamp()
    )
    assert p.description == ""


def test_breezy_board_with_no_openings_is_empty(monkeypatch):
    _serve(monkeypatch, BoardResponse([]))
    assert boards.fetch_listings(_URL, "JND") == []


def test_breezy_reply_that_is_not_a_list_fails_the_pull(monkeypatch):
    _serve(monkeypatch, BoardResponse({"error": "Not Found"}))
    with pytest.raises(ValueError):
        boards.fetch_listings(_URL, "JND")
