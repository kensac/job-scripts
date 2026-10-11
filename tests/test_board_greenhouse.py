"""Greenhouse board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import datetime

from core.fetching import boards
from tests.board_responses import BoardResponse as _Resp


def test_greenhouse_names_the_company_from_the_board(monkeypatch):
    # boards-api.greenhouse.io/v1/boards/andurilindustries/jobs, first job.
    body = {
        "jobs": [
            {
                "absolute_url": "https://boards.greenhouse.io/andurilindustries/jobs/4802172007?gh_jid=4802172007",
                "company_name": "Anduril Industries",
                "first_published": "2025-08-11T13:41:15-04:00",
                "id": 4802172007,
                "location": {
                    "name": "Costa Mesa, California, United States; Fort Collins, Colorado, United States"
                },
                "title": "2026 Early Career Electrical Engineer",
                "updated_at": "2026-09-02T20:46:09-04:00",
                "content": "&lt;p&gt;Build &amp;amp; test avionics.&lt;/p&gt;",
            }
        ]
    }
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: asked.append(url) or _Resp(body))
    (p,) = boards.fetch_listings(
        "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs", company="ignored"
    )
    # One call, with the text: nothing downstream needs to fetch the posting.
    assert asked == [
        "https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs?content=true"
    ]
    assert p.description == (
        "2026 Early Career Electrical Engineer\n\n"
        "Costa Mesa, California, United States; Fort Collins, Colorado, United States\n\n"
        "Build & test avionics."
    )
    assert p.raw is not None and p.raw["id"] == 4802172007 and "content" not in p.raw
    assert p.company == "Anduril Industries"
    assert p.locations == [
        "Costa Mesa, California, United States",
        "Fort Collins, Colorado, United States",
    ]
    assert p.url == "https://boards.greenhouse.io/andurilindustries/jobs/4802172007"
    assert p.date_posted == int(
        datetime.datetime.fromisoformat("2025-08-11T13:41:15-04:00").timestamp()
    )
