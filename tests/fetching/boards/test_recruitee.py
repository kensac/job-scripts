"""Recruitee board behavior over Hard Rock Digital's offers API as served on
2026-11-01, trimmed to one offer with shortened text."""

from __future__ import annotations

import datetime

import pytest
import requests

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse

_URL = "https://hardrockdigital.recruitee.com/api/offers/"

_OFFER = {
    "id": 2803651,
    "guid": "pkdna",
    "slug": "coordinator-casino-promotions",
    "title": "Coordinator - Casino Promotions",
    "company_name": "Hard Rock Digital",
    "department": "Marketing",
    "location": "Remote job",
    "locations": [
        {"id": 270241, "name": "United States", "state": "Florida", "country": "United States"}
    ],
    "published_at": "2026-10-30 10:16:53 UTC",
    "careers_url": "https://hardrockdigital.recruitee.com/o/coordinator-casino-promotions",
    "status": "published",
    "remote": True,
    "description": "<p><strong>What are we building?</strong></p><p>Hard Rock Digital is a "
    "team working to become the best online sportsbook.</p>",
    "requirements": "<p><strong>What are we looking for?</strong></p><ul><li>2+ years in "
    "casino promotions</li></ul>",
    "translations": {"en": {"description": "<p>What are we building?</p>"}},
    "open_questions": [{"id": 3326955, "body": "Do you/have you worked for Hard Rock?"}],
}


def _serve(monkeypatch, response):
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: asked.append(url) or response)
    return asked


def test_recruitee_reads_every_offer_with_its_text_in_one_call(monkeypatch):
    asked = _serve(monkeypatch, BoardResponse({"offers": [_OFFER]}))
    (p,) = boards.fetch_listings(_URL, "Hard Rock Digital")
    assert asked == [_URL]
    assert boards.kind(_URL) == "recruitee"
    assert p.url == "https://hardrockdigital.recruitee.com/o/coordinator-casino-promotions"
    assert p.locations == ["United States"]
    assert p.date_posted == int(
        datetime.datetime(2026, 10, 30, 10, 16, 53, tzinfo=datetime.UTC).timestamp()
    )
    assert p.description == (
        "Coordinator - Casino Promotions\n\nUnited States\n\nWhat are we building?\n"
        "Hard Rock Digital is a team working to become the best online sportsbook.\n\n"
        "What are we looking for?\n2+ years in casino promotions"
    )
    assert p.raw is not None
    assert not {"description", "requirements", "translations", "open_questions"} & set(p.raw)


def test_recruitee_board_with_no_openings_is_empty(monkeypatch):
    _serve(monkeypatch, BoardResponse({"offers": []}))
    assert boards.fetch_listings(_URL, "Hard Rock Digital") == []


def test_recruitee_tenant_that_left_fails_the_pull(monkeypatch):
    # 1X's tenant answered 404 {"error":"Not Found"} once it left Recruitee.
    class Missing(BoardResponse):
        def raise_for_status(self):
            raise requests.HTTPError("404")

    _serve(monkeypatch, Missing({"error": "Not Found"}))
    with pytest.raises(requests.HTTPError):
        boards.fetch_listings(_URL, "Hard Rock Digital")


def test_recruitee_reply_without_offers_fails_the_pull(monkeypatch):
    _serve(monkeypatch, BoardResponse({"error": "Not Found"}))
    with pytest.raises(KeyError):
        boards.fetch_listings(_URL, "Hard Rock Digital")
