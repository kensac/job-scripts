"""Taleo board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.board_responses import BoardResponse as _Resp

# Rows as textron.taleo.net, aarcorp.taleo.net and baesystems.taleo.net
# returned them on 2026-10-05. Which column is which is per careersection:
# Textron and AAR send title, locations and date, BAE the title alone, and
# the locations cell is a JSON list inside a string.
_TALEO_ROWS = [
    {
        "hotJob": False,
        "jobId": "1540116",
        "contestNo": "342919",
        "column": [
            "2027 - Systems Engineer (Uncrewed Land & Air) - Hunt Valley, MD",
            '["US-Maryland-Hunt Valley"]',
            "10/02/2026",
        ],
        "linkedColumn": 0,
        "locationsColumns": [1],
    },
    {
        "hotJob": False,
        "jobId": "326546",
        "contestNo": "18884",
        "column": ["A&P Certificated Apprentice", '["United States-Florida-Miami"]', "Oct 5, 2026"],
        "linkedColumn": 0,
        "locationsColumns": [1],
    },
    {
        "hotJob": True,
        "jobId": "1014544",
        "contestNo": "00110645",
        "column": ["Metrology Engineer (Calibration)"],
        "linkedColumn": 0,
        "locationsColumns": [],
    },
]


def _taleo_board(monkeypatch, pages: list[list[dict]], total: int):
    """The search endpoint as it answered live: 25-row pages, the same
    totalCount on every page, and any page past the last answered with the
    last page again (Kautex, pages 5 through 500), so a loop that waits for
    an empty page never ends. More requests than pages fails the test
    instead of hanging it."""
    asked = []

    def post(url, json, headers, **kw):
        assert headers.get("tz"), "without a tz header the endpoint answers 500"
        asked.append((url, json["pageNo"]))
        assert len(asked) <= len(pages), "paged past the last page"
        number = min(json["pageNo"], len(pages))
        return _Resp(
            {
                "requisitionList": pages[number - 1],
                "pagingData": {
                    "currentPageNo": json["pageNo"],
                    "pageSize": 25,
                    "totalCount": total,
                },
            }
        )

    monkeypatch.setattr(boards._session, "post", post)
    return asked


def _taleo_rows(n: int, start: int = 0) -> list[dict]:
    return [
        {**_TALEO_ROWS[2], "contestNo": str(start + i), "column": [f"Role {start + i}"]}
        for i in range(n)
    ]


def test_taleo_pages_to_the_count_and_builds_the_public_url(monkeypatch):
    pages = [_TALEO_ROWS + _taleo_rows(22), _taleo_rows(1, start=22)]
    asked = _taleo_board(monkeypatch, pages, total=26)
    page_html = "<script>var settings = { portalNo: '8140753014', lang: 'en' };</script>"
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _Resp(page_html))
    out = boards.fetch_listings(
        "https://textron.taleo.net/careersection/textron/jobsearch.ftl?lang=en", "Textron"
    )
    assert len(out) == 26
    first, aar, bae = out[:3]
    assert (first.title, first.locations, first.date_posted) == (
        "2027 - Systems Engineer (Uncrewed Land & Air) - Hunt Valley, MD",
        ["US-Maryland-Hunt Valley"],
        int(datetime.datetime(2026, 10, 2, tzinfo=datetime.UTC).timestamp()),
    )
    assert first.url == "https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=342919"
    assert aar.date_posted == int(datetime.datetime(2026, 10, 5, tzinfo=datetime.UTC).timestamp())
    assert (bae.title, bae.locations, bae.date_posted) == (
        "Metrology Engineer (Calibration)",
        [],
        0,
    )
    assert bae.url.endswith("jobdetail.ftl?job=00110645")
    assert all(p.company == "Textron" for p in out)
    # The portal read off the search page rode on the endpoint, one request a
    # page, and nothing past the second.
    assert asked == [
        (
            "https://textron.taleo.net/careersection/rest/jobboard/searchjobs"
            "?lang=en&portal=8140753014",
            n,
        )
        for n in (1, 2)
    ]


def test_a_taleo_pull_short_of_its_count_is_partial(monkeypatch):
    """Textron on 2026-10-05: 751 counted, 691 listed. Pages run short in the
    middle (24 of 25) and the last page the count implies came back empty.
    A loop that stops on the first short page sees 24; one that trusts what
    it saw retires the 11 it was never shown."""
    pages = [_taleo_rows(24), _taleo_rows(25, start=24), []]
    _taleo_board(monkeypatch, pages, total=60)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(
            "https://textron.taleo.net/careersection/textron/jobsearch.ftl?lang=en&portal=1",
            "Textron",
        )
    assert len(raised.value.postings) == 49
