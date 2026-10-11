"""Apple board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse as _Resp


class _AppleSearch:
    """jobs.apple.com/api/v1/search as measured on 2026-10-05.

    20 rows a page, the count on every page and 0 on the empty one past the
    end. A body without "format" gets that empty page from the start. The
    managed pipeline roles sort first and their order is drawn afresh by each
    request, here a rotation by five per call, so one pass over them returns
    some twice and misses others. `hidden` rows are counted but never served.
    """

    def __init__(self, managed: int, regular: int, hidden: int = 0):
        self.managed = [
            {
                "id": f"PIPE-1144380{i:02d}",
                "postingTitle": f"Expert {i}",
                "transformedPostingTitle": f"expert-{i}",
                "managedPipelineRole": True,
                "postDateInGMT": "2026-10-05T05:21:50.895303331Z",
                "locations": [{"name": "India", "countryName": "India"}],
            }
            for i in range(managed)
        ]
        self.regular = [
            {
                "id": f"2006865{i:02d}-3916",
                "postingTitle": f"Engineer {i}",
                "transformedPostingTitle": f"engineer-{i}",
                "managedPipelineRole": False,
                "postDateInGMT": "2026-10-04T18:02:11.304Z",
                "locations": [{"name": "Cupertino", "countryName": "United States of America"}],
            }
            for i in range(regular)
        ]
        self.total = managed + regular + hidden
        self.bodies: list[dict] = []

    def post(self, url, json, **kw):
        assert url == "https://jobs.apple.com/api/v1/search"
        shift = 5 * len(self.bodies) % len(self.managed)
        self.bodies.append(json)
        rows = self.managed[shift:] + self.managed[:shift] + self.regular
        start = (json["page"] - 1) * boards._APPLE_PAGE
        page = rows[start : start + boards._APPLE_PAGE] if "format" in json else []
        return _Resp({"res": {"searchResults": page, "totalRecords": self.total if page else 0}})


def test_apple_reads_the_unstable_pages_again_until_every_posting_is_seen(monkeypatch):
    """One pass here returns 45 rows of 45 with five of them twice and five
    never, as one pass of the live board did on 2026-10-05 (6,192 rows, 6,190
    distinct). The fetcher must ask with "format", see the distinct count fall
    short, and re-read the pages the managed roles held."""
    board = _AppleSearch(managed=25, regular=20)
    monkeypatch.setattr(boards._session, "post", board.post)
    out = boards.fetch_listings("https://jobs.apple.com/api/v1/search", "Apple")
    assert len({p.url for p in out}) == len(out) == 45
    # Three pages for one pass, then the two that held managed roles.
    assert [b["page"] for b in board.bodies] == [1, 2, 3, 1, 2]
    assert all(p.company == "Apple" for p in out)
    expert = next(p for p in out if p.title == "Expert 0")
    assert expert.url == "https://jobs.apple.com/en-us/details/114438000/expert-0"
    assert expert.locations == ["India"]
    assert expert.date_posted == 0, "a managed role's timestamp is the request's"
    engineer = next(p for p in out if p.title == "Engineer 0")
    assert engineer.url == "https://jobs.apple.com/en-us/details/200686500-3916/engineer-0"
    assert engineer.locations == ["Cupertino, United States of America"]
    assert engineer.date_posted == int(
        datetime.datetime(2026, 10, 4, 18, 2, 11, 304000, tzinfo=datetime.UTC).timestamp()
    )


def test_apple_short_of_its_count_is_a_partial_pull(monkeypatch):
    board = _AppleSearch(managed=25, regular=20, hidden=1)
    monkeypatch.setattr(boards._session, "post", board.post)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://jobs.apple.com/api/v1/search", "Apple")
    assert len(raised.value.postings) == 45
    # Re-reading stops on the first round that finds nothing new.
    assert [b["page"] for b in board.bodies] == [1, 2, 3, 1, 2, 1, 2]
