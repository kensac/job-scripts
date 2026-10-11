"""Ashby board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse as _Resp


def test_ashby_takes_the_company_from_the_source(monkeypatch):
    ashby = {
        "apiVersion": "1",
        "jobs": [
            {
                "isListed": True,
                "jobUrl": "https://jobs.ashbyhq.com/quora/b2462dc7-08d7-4060-8ceb-9a1fa16615fb",
                "location": "Remote - Multiple Locations",
                "publishedAt": "2026-05-18T21:03:00.213+00:00",
                "secondaryLocations": [{"location": "United States"}, {"location": "Canada"}],
                "title": "Detection & CorpSec Engineer (Remote)",
                "descriptionHtml": "<p>Hunt threats.</p>",
                "compensation": {"compensationTierSummary": "$150K to $200K"},
            },
            {"isListed": False, "jobUrl": "https://jobs.ashbyhq.com/quora/x", "title": "Hidden"},
        ],
    }
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: asked.append(url) or _Resp(ashby))
    (q,) = boards.fetch_listings("https://api.ashbyhq.com/posting-api/job-board/quora", "Quora")
    assert q.company == "Quora"
    assert q.locations == ["Remote - Multiple Locations", "United States", "Canada"]
    assert (
        asked[-1] == "https://api.ashbyhq.com/posting-api/job-board/quora?includeCompensation=true"
    )
    assert q.description == (
        "Detection & CorpSec Engineer (Remote)\n\nRemote - Multiple Locations\n\n"
        "$150K to $200K\n\nHunt threats."
    )
