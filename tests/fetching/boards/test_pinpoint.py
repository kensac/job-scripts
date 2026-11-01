"""Pinpoint board behavior over Wolverine Trading's postings.json as served on
2026-11-01, trimmed to one posting with shortened sections."""

from __future__ import annotations

import pytest
import requests

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse

_URL = "https://wolve.pinpointhq.com/postings.json"

_ROW = {
    "id": "446898",
    "benefits": "<div><!--block-->Highly competitive salary &amp; bonus opportunity</div>",
    "benefits_header": "Why Join Us",
    "compensation": None,
    "deadline_at": None,
    "description": "<div><!--block-->At Wolverine Trading we\u2019re looking for a Low-Level C++ "
    "Software Engineer.</div>",
    "employment_type": "full_time",
    "employment_type_text": "Full Time",
    "key_responsibilities": "<ul><li><!--block-->Develop performance-critical C++ "
    "applications</li></ul>",
    "key_responsibilities_header": "What You\u2019ll Do",
    "reporting_to": "",
    "skills_knowledge_expertise": "<ul><li><!--block-->Bachelor's degree in Computer "
    "Science</li></ul>",
    "skills_knowledge_expertise_header": "What We\u2019re Looking For",
    "title": "C++ Software Engineer",
    "url": "https://careers.wolve.com/en/postings/856f5215-88c8-4c1b-bd47-5106b33ed37f",
    "path": "/en/postings/856f5215-88c8-4c1b-bd47-5106b33ed37f",
    "workplace_type": "onsite",
    "job": {"id": "455116", "requisition_id": "RID-0032"},
    "location": {"id": "66266", "city": "Chicago", "name": "Chicago, IL"},
}


def _serve(monkeypatch, response):
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: asked.append(url) or response)
    return asked


def test_pinpoint_reads_every_posting_with_its_sections_in_one_call(monkeypatch):
    asked = _serve(monkeypatch, BoardResponse({"data": [_ROW]}))
    (p,) = boards.fetch_listings(_URL, "Wolverine Trading")
    assert asked == [_URL]
    assert boards.kind(_URL) == "pinpoint"
    # The tenant host, which the aggregators link, not the employer's domain.
    assert p.url == (
        "https://wolve.pinpointhq.com/en/postings/856f5215-88c8-4c1b-bd47-5106b33ed37f"
    )
    assert p.locations == ["Chicago, IL"]
    assert p.description == (
        "C++ Software Engineer\n\nChicago, IL\n\n"
        "At Wolverine Trading we're looking for a Low-Level C++ Software Engineer.\n\n"
        "What You'll Do\n\nDevelop performance-critical C++ applications\n\n"
        "What We're Looking For\n\nBachelor's degree in Computer Science\n\n"
        "Why Join Us\n\nHighly competitive salary & bonus opportunity"
    )
    assert p.raw is not None
    assert not {"description", "benefits", "key_responsibilities"} & set(p.raw)
    assert p.raw["job"]["requisition_id"] == "RID-0032"


def test_pinpoint_board_with_no_openings_is_empty(monkeypatch):
    _serve(monkeypatch, BoardResponse({"data": []}))
    assert boards.fetch_listings(_URL, "Wolverine Trading") == []


def test_pinpoint_reply_without_data_fails_the_pull(monkeypatch):
    _serve(monkeypatch, BoardResponse({"error": "not found"}))
    with pytest.raises(KeyError):
        boards.fetch_listings(_URL, "Wolverine Trading")


def test_pinpoint_unknown_tenant_fails_the_pull(monkeypatch):
    class Missing(BoardResponse):
        def raise_for_status(self):
            raise requests.HTTPError("404")

    _serve(monkeypatch, Missing(""))
    with pytest.raises(requests.HTTPError):
        boards.fetch_listings(_URL, "Wolverine Trading")
