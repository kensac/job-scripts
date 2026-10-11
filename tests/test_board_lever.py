"""Lever board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

from core.fetching import boards
from tests.board_responses import BoardResponse as _Resp


def test_lever_takes_the_company_from_the_source(monkeypatch):
    lever = [
        {
            "categories": {
                "allLocations": ["London, United Kingdom"],
                "location": "London, United Kingdom",
            },
            "createdAt": 1711403416463,
            "hostedUrl": "https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c",
            "text": "Administrative Business Partner",
            "description": "<p>Support the team.</p>",
            "lists": [{"text": "What you bring", "content": "<li>Calm</li>"}],
            "additional": "",
        }
    ]
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: asked.append(url) or _Resp(lever))
    (p,) = boards.fetch_listings("https://api.lever.co/v0/postings/palantir?mode=json", "Palantir")
    assert (p.company, p.title, p.date_posted) == (
        "Palantir",
        "Administrative Business Partner",
        1711403416,
    )
    # The same text the Lever resolver assembles: title, location, body, lists.
    assert p.description == (
        "Administrative Business Partner\n\nLondon, United Kingdom\n\n"
        "Support the team.\n\nWhat you bring\n\nCalm"
    )
    assert p.raw is not None and "description" not in p.raw and "lists" not in p.raw
    assert asked == ["https://api.lever.co/v0/postings/palantir?mode=json"]
