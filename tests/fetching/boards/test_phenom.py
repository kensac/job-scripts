"""Phenom board behavior over producer-shaped search replies."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse as _Resp

# careers.franklintempleton.com/widgets, refineSearch, 2026-11-01: two rows
# verbatim, cut to the fields the fetcher reads and a few beside them.
_FT_ROWS = [
    {
        "jobId": "869864",
        "jobSeqNo": "FTGFTMUS869864EXTERNALENUS",
        "title": "Systematic Equity Analyst (BW) \u2013 Franklin Equity",
        "postedDate": "2026-10-08T00:00:00.000+0000",
        "multi_location": ["Philadelphia, Pennsylvania, United States of America"],
        "location": "Philadelphia, Pennsylvania, United States of America",
        "cityStateCountry": "Philadelphia, Pennsylvania, United States of America",
        "descriptionTeaser": "Become part of our team as a Systematic Equity Analyst.",
        "applyUrl": "https://franklintempleton.wd5.myworkdayjobs.com/Primary-External-1/job/"
        "Philadelphia-Pennsylvania-United-States-of-America/"
        "Systematic-Equity-Analyst--BW----Franklin-Equity_869864/apply",
        "category": "Investment Management",
        "type": "Full time",
        "reqId": "869864",
    },
    {
        "jobId": "869719",
        "jobSeqNo": "FTGFTMUS869719EXTERNALENUS",
        "title": "SR HR Shared Services Specialist",
        "postedDate": "2026-09-25T00:00:00.000+0000",
        "multi_location": ["New York, New York, United States of America"],
        "location": "New York, New York, United States of America",
        "cityStateCountry": "New York, New York, United States of America",
        "category": "Corporate Services",
        "type": "Full time",
        "reqId": "869719",
    },
]
_FT_SEARCH = "https://careers.franklintempleton.com/us/en/search-results"


def _rows(n: int, start: int = 0) -> list[dict]:
    return [
        {**_FT_ROWS[1], "jobId": str(start + i), "title": f"Role {start + i}"} for i in range(n)
    ]


def _reply(jobs: list[dict] | None, total: int) -> dict:
    data = {} if jobs is None else {"jobs": jobs, "suggestions": {}}
    return {
        "refineSearch": {"status": 200, "hits": len(jobs or []), "totalHits": total, "data": data}
    }


def _phenom_board(monkeypatch, rows: list[dict], total: int | None = None, reply=None):
    """The search as it answered live: up to 500 rows from `from`, the count on
    every page."""
    asked = []

    def post(url, json, **kw):
        asked.append((url, json))
        if reply is not None:
            return _Resp(reply)
        page = rows[json["from"] : json["from"] + min(json["size"], 500)]
        return _Resp(_reply(page, len(rows) if total is None else total))

    monkeypatch.setattr(boards._session, "post", post)
    return asked


def test_phenom_reads_a_board_that_fits_in_one_call(monkeypatch):
    asked = _phenom_board(monkeypatch, _FT_ROWS)
    out = boards.fetch_listings(_FT_SEARCH, "Franklin Templeton")
    assert asked == [
        (
            "https://careers.franklintempleton.com/widgets",
            {
                "ddoKey": "refineSearch",
                "from": 0,
                "size": 500,
                "jobs": True,
                "counts": False,
                "lang": "en_us",
                "country": "us",
            },
        )
    ]
    analyst, hr = out
    assert (analyst.title, analyst.locations, analyst.company) == (
        "Systematic Equity Analyst (BW) \u2013 Franklin Equity",
        ["Philadelphia, Pennsylvania, United States of America"],
        "Franklin Templeton",
    )
    assert analyst.url == "https://careers.franklintempleton.com/us/en/job/869864"
    assert analyst.date_posted == int(
        datetime.datetime(2026, 10, 8, tzinfo=datetime.UTC).timestamp()
    )
    # A teaser is not the posting, so no text is stored.
    assert analyst.description == ""
    assert hr.url.endswith("/us/en/job/869719")


def test_phenom_pages_a_larger_board_to_its_count(monkeypatch):
    asked = _phenom_board(monkeypatch, _rows(1_203))
    out = boards.fetch_listings("https://career.lpl.com/search-results", "LPL Financial")
    assert len(out) == 1_203
    assert [body["from"] for _, body in asked] == [0, 500, 1_000]
    # A site without a language prefix is asked in its default.
    assert "lang" not in asked[0][1]
    assert out[0].url == "https://career.lpl.com/job/0"


def test_phenom_pull_short_of_its_count_is_partial(monkeypatch):
    # Pages that shifted between replicas: the third repeats rows already seen.
    rows = _rows(1_000) + _rows(203)
    _phenom_board(monkeypatch, rows)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(_FT_SEARCH, "Franklin Templeton")
    assert len(raised.value.postings) == 1_000


def test_an_empty_phenom_board_is_empty(monkeypatch):
    _phenom_board(monkeypatch, [])
    assert boards.fetch_listings(_FT_SEARCH, "Franklin Templeton") == []


@pytest.mark.parametrize(
    "reply",
    [
        # What it answered a request it could not read ("from": "x").
        _reply(None, 0),
        {"refineSearch": {"status": 500, "totalHits": 0, "data": {"jobs": []}}},
    ],
)
def test_a_phenom_reply_without_a_job_list_fails_the_pull(monkeypatch, reply):
    _phenom_board(monkeypatch, [], reply=reply)
    with pytest.raises(ValueError, match="no job list"):
        boards.fetch_listings(_FT_SEARCH, "Franklin Templeton")
