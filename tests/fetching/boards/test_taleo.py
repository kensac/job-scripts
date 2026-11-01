"""Taleo board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse as _Resp

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


# kearney.taleo.net/careersection/01: jobsearch.ftl redirects to joblist.ftl,
# a server-rendered list with no portalNo. The page states the list's size
# and length; its pager, joblist.ajax, answers each page as cells. Verbatim
# from 2026-11-01, the page cut to what the fetcher reads.
_LIST_URL = "https://kearney.taleo.net/careersection/01/joblist.ftl?lang=en"
_LIST_PAGE = (
    '<form name="ftlform" id="ftlform" method="post" action="joblist.ftl">'
    '<input type="hidden" name="ftlpageid" id="ftlpageid" value="reqListAllJobsPage">'
    '<input type="hidden" name="initialHistory" id="initialHistory" value="'
    "listRequisition.size!|!10!|!listRequisition.nbElements!|!{total}!|!listRequisition.isEmpty"
    '!|!false">'
)
_PAGER_HEAD = (
    "ftlx1!|!ftlPager_processResponse!$!requisitionListInterface!|!rlPager!|!listRequisition"
    "!|!5!|!150769!|!5!|!Go to page {0}!|!Go to page {0}!|!The list of jobs has reloaded. "
    "{0} \u2013 {1} of {2}. Page {3}!|! !|!Previous!|! !|!Go to the previous page!|! !|!Next!|! "
    "!|!Go to the next page!|! !|! !$!false"
)
_KEARNEY_ROWS = [
    "150769!|!Consultant, Kearney Activate - Prokura !|!150769!|!Consultant, Kearney Activate - "
    "Prokura !|!150769!|!150769!|!150769!|!150769!|!150769!|!Full-time!|!Americas-United "
    "States-Dallas!|!false!|!!|!!|!!|!!|!Oct 27, 2026!|!006OR!|!!|!false!|!Apply!|!Apply for "
    "this position (Consultant, Kearney Activate - Prokura )!|!150769!|!true!|!Re-apply!|!"
    "Re-apply for this job!|!150769!|!false!|!false!|!150769!|!false!|!false!|!Submission for "
    "the position%5C: Consultant, Kearney Activate - Prokura  - (Job Number%5C: 006OR)!|!false"
    "!|!true!|!Add to My Job Cart!|!Add this position to the job cart%5C: Consultant, Kearney "
    "Activate - Prokura !|!150769!|!false!|!true!|!false!|!false",
    "149814!|!Kearney Senior Business Analyst, Communications, Media %26 Technology !|!149814!|!"
    "Kearney Senior Business Analyst, Communications, Media %26 Technology !|!149814!|!149814!|!"
    "149814!|!149814!|!149814!|!Full-time!|!Americas-United States-Chicago, Americas-United "
    "States-Dallas, Americas-United States-Washington D.C.!|!true!|!Position available in other "
    "locations%5C: Americas-United States-Atlanta, Americas-Canada-Toronto!|!More...!|!Position "
    "available in other locations%5C: Americas-United States-Atlanta, Americas-Canada-Toronto"
    "!|!, Americas-United States-Atlanta, Americas-Canada-Toronto!|!Sep 18, 2026!|!006MN!|!!|!"
    "false!|!Apply!|!Apply for this position (Kearney Senior Business Analyst, Communications, "
    "Media %26 Technology )!|!149814!|!true!|!Re-apply!|!Re-apply for this job!|!149814!|!false"
    "!|!false!|!149814!|!false!|!false!|!Submission for the position%5C: Kearney Senior Business"
    " Analyst, Communications, Media %26 Technology  - (Job Number%5C: 006MN)!|!false!|!true!|!"
    "Add to My Job Cart!|!Add this position to the job cart%5C: Kearney Senior Business Analyst,"
    " Communications, Media %26 Technology !|!149814!|!false!|!true!|!false!|!false",
]
_PAGER_TAIL = "rlPager.currentPage!|!5!|!rlPager.pagerLabelNext!|!Next"


def _kearney_row(n: int) -> str:
    """Kearney's first row with its own number, title and code."""
    return (
        _KEARNEY_ROWS[0]
        .replace("150769", str(140000 + n))
        .replace("006OR", f"00{n:03d}")
        .replace("Prokura", f"Prokura {n}")
    )


class _ListPage(_Resp):
    url = _LIST_URL


def _list_board(monkeypatch, total: int, pages: list[list[str]]):
    """The page, and its pager answering page n with pages[n - 1]."""
    asked = []
    monkeypatch.setattr(
        boards._session,
        "get",
        lambda url, **kw: _ListPage(_LIST_PAGE.replace("{total}", str(total))),
    )

    def post(url, data, **kw):
        asked.append((url, data["rlPager.currentPage"], data["ftlpageid"]))
        rows = pages[int(data["rlPager.currentPage"]) - 1]
        return _Resp("!|!".join([_PAGER_HEAD, *rows, _PAGER_TAIL]))

    monkeypatch.setattr(boards._session, "post", post)
    return asked


def test_a_server_rendered_taleo_list_is_read_page_by_page_from_its_pager(monkeypatch):
    pages = [[_kearney_row(n) for n in range(10)], _KEARNEY_ROWS]
    asked = _list_board(monkeypatch, 12, pages)
    out = boards.fetch_listings(
        "https://kearney.taleo.net/careersection/01/jobsearch.ftl?lang=en", "Kearney"
    )
    assert len(out) == 12
    # Every page from the pager, the first too, at the page the redirect landed on.
    assert asked == [
        ("https://kearney.taleo.net/careersection/01/joblist.ajax", str(n), "reqListAllJobsPage")
        for n in (1, 2)
    ]
    prokura, analyst = out[-2:]
    assert (prokura.title, prokura.locations, prokura.company) == (
        "Consultant, Kearney Activate - Prokura",
        [],
        "Kearney",
    )
    assert prokura.url == "https://kearney.taleo.net/careersection/01/jobdetail.ftl?job=006OR"
    assert prokura.date_posted == int(
        datetime.datetime(2026, 10, 27, tzinfo=datetime.UTC).timestamp()
    )
    assert analyst.title == "Kearney Senior Business Analyst, Communications, Media & Technology"
    assert analyst.url.endswith("job=006MN")


def test_a_server_rendered_taleo_list_short_of_its_length_is_partial(monkeypatch):
    _list_board(monkeypatch, 12, [[_kearney_row(n) for n in range(10)], _KEARNEY_ROWS[:1]])
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(
            "https://kearney.taleo.net/careersection/01/jobsearch.ftl?lang=en", "Kearney"
        )
    assert len(raised.value.postings) == 11


def test_an_empty_server_rendered_taleo_list_is_an_empty_board(monkeypatch):
    asked = _list_board(monkeypatch, 0, [])
    assert boards.fetch_listings(_LIST_URL.replace("joblist", "jobsearch"), "Kearney") == []
    assert asked == []


def test_a_taleo_page_that_is_neither_search_nor_list_fails(monkeypatch):
    # Vulcan Materials' careersection, 2026-11-01.
    unavailable = "<html><head><title>Career Section Unavailable</title></head></html>"
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _ListPage(unavailable))
    with pytest.raises(ValueError, match="neither a faceted search nor a job list"):
        boards.fetch_listings(
            "https://vulcanmat.taleo.net/careersection/2/jobsearch.ftl?lang=en", "Vulcan"
        )
