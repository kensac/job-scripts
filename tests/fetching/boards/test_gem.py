"""Gem board behavior over a reply copied from the live API on 2026-10-16
(Function Health) and trimmed to the keys read."""

from __future__ import annotations

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse as _Resp

_URL = "https://api.gem.com/job_board/v0/function-health/job_posts/"


def test_gem_reads_every_posting_with_its_text_in_one_call(monkeypatch):
    gem = [
        {
            "absolute_url": "https://jobs.gem.com/function-health/am9icG9zdDramAtjvaEx5CaiubYzZIC0",
            "content": "<h3><strong>Company Overview</strong></h3><div>Function Health is "
            "the AI operating system for health.</div>",
            "content_plain": "Company Overview\nFunction Health is the AI operating system "
            "for health.",
            "first_published_at": "2026-01-28T20:32:42.485Z",
            "id": "am9icG9zdDramAtjvaEx5CaiubYzZIC0",
            "location": {"name": "United States - Remote"},
            "offices": [{"name": "US - Remote", "location": {"name": "United States - Remote"}}],
            "title": "Principal Product Designer",
        },
        {"absolute_url": "https://jobs.gem.com/function-health/x", "title": ""},
    ]
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: asked.append(url) or _Resp(gem))
    (p,) = boards.fetch_listings(_URL, "Function Health")
    assert asked == [_URL]
    assert p.company == "Function Health"
    assert p.url == "https://jobs.gem.com/function-health/am9icG9zdDramAtjvaEx5CaiubYzZIC0"
    assert p.locations == ["United States - Remote"]
    assert p.date_posted == 1769632362
    assert p.description == (
        "Principal Product Designer\n\nUnited States - Remote\n\nCompany Overview\n"
        "Function Health is the AI operating system for health."
    )
    # Both copies of the text stay out of the raw record.
    assert p.raw is not None
    assert "content" not in p.raw and "content_plain" not in p.raw
