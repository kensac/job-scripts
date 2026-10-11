"""Icims board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import datetime

import pytest

from core.fetching import boards
from tests.board_responses import BoardResponse as _Resp

# Joby card whose locations sit in the header, joined by " | ". Verbatim but
# for the description snippet, which the fetcher does not read.
_GDMS_CARD = """
<li class="iCIMS_JobCardItem">
<div class="row">
<div class="col-xs-6 header left">
</div>
<div class="col-xs-6 header right">
<span class="sr-only field-label">Posted Date</span>
<span title="10/4/2026 5:15 PM">
8 hours ago<span class="sr-only">(10/4/2026 5:15 PM)</span></span>
</div>
<div class="col-xs-12 title">
<a href="https://careers-gdms.icims.com/jobs/{id}/sr.-advanced-field-support-specialist-%28ts-sci-clearance-required%29/job?in_iframe=1" class="iCIMS_Anchor" title="{id} - {title}">
<span class="sr-only field-label">Title</span>
<h3 >
{title}</h3>
</a>
</div>
<div class="col-xs-12 description">
Bachelor's degree in a related specialized area...</div>
<div class="col-xs-12 additionalFields">
<dl class="iCIMS_JobHeaderGroup">
<div class="iCIMS_JobHeaderTag">
<dt class="iCIMS_JobHeaderField">ID</dt>
<dd class="iCIMS_JobHeaderData"><span >
2026-{id}</span>
</dd>
</div>
<div class="iCIMS_JobHeaderTag">
<dt class="iCIMS_JobHeaderField"><span class="glyphicons glyphicons-map-marker" aria-hidden="true"></span>
<span class="sr-only field-label">Job Location</span>
</dt>
<dd class="iCIMS_JobHeaderData"><span >
US-MD-Annapolis Junction</span>
</dd>
</div>
<div class="iCIMS_JobHeaderTag">
<dt class="iCIMS_JobHeaderField">Required Clearance</dt>
<dd class="iCIMS_JobHeaderData"><span >
TS/SCI</span>
</dd>
</div>
</dl>
</div>
</div>
</li>
"""

_JOBY_CARD = """
<li class="iCIMS_JobCardItem">
<div class="row">
<div class="col-xs-6 header left">
<span class="sr-only field-label">Job Locations</span>
<span >
US-CA-Marina | US-CA-Watsonville</span>
</div>
<div class="col-xs-6 header right">
</div>
<div class="col-xs-12 title">
<a href="https://careers-jobyaviation.icims.com/jobs/{id}/aircraft-maintenance-training-manager/job?in_iframe=1" class="iCIMS_Anchor" title="{id} - {title}">
<span class="sr-only field-label">Title</span>
<h3 >
{title}</h3>
</a>
</div>
<div class="col-xs-12 additionalFields">
<dl class="iCIMS_JobHeaderGroup">
<div class="iCIMS_JobHeaderTag">
<dt class="iCIMS_JobHeaderField">Category</dt>
<dd class="iCIMS_JobHeaderData"><span >
Air Operations</span>
</dd>
</div>
</dl>
</div>
</div>
</li>
"""


def _icims_page(card: str, ids: list[int], page: int, pages: int) -> str:
    """One search page as the portal serves it. Past the last page it answers
    200 with neither cards nor the "Page N of M" heading (GDMS pr=35, Peraton
    pr=31, Joby pr=8, 2026-10-05). The search form above the results carries
    the same heading class with no count in it (Expleo)."""
    if page >= pages:
        return '<div class="iCIMS_SearchResultsHeader"></div>'
    cards = "".join(card.format(id=i, title=f"Role {i}") for i in ids)
    return (
        '<p class="iCIMS_SubHeader iCIMS_SubHeader_Jobs">Search by keyword</p>'
        '<div class="container-fluid iCIMS_SearchResultsHeader"><div class="row">'
        '<div class="pull-left"><h2 class="iCIMS_SubHeader iCIMS_SubHeader_Jobs">\n'
        f"Search Results\nPage {page + 1} of {pages} \n</h2></div></div></div>"
        f'<ul class="container-fluid iCIMS_JobsTable">{cards}</ul>'
    )


def _serve_icims(monkeypatch, card: str, by_page: dict[int, list[int]], pages: int):
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        pr = int(url.split("pr=")[1].split("&")[0])
        return _Resp(_icims_page(card, by_page.get(pr, []), pr, pages))

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_icims_pages_by_zero_based_pr_to_the_count_its_first_page_states(monkeypatch):
    """pr= is zero-based where the heading is one-based, and the page size is
    the tenant's (GDMS 20, Electric Boat 50), so the stated page count is what
    ends the pull. A loop that started at pr=1, or stopped on a short or empty
    page of an assumed size, asks a different sequence."""
    asked = _serve_icims(monkeypatch, _GDMS_CARD, {0: [1, 2, 3], 1: [4, 5, 6], 2: [7]}, pages=3)
    out = boards.fetch_listings(
        "https://careers-gdms.icims.com/jobs/search", "General Dynamics Mission Systems"
    )
    assert [p.title for p in out] == [f"Role {i}" for i in range(1, 8)]
    assert [u.split("pr=")[1] for u in asked] == ["0", "1", "2"]
    assert all("in_iframe=1" in u and "ss=1" in u for u in asked)
    p = out[0]
    # The posting a person opens: the portal page, without the iframe flag or
    # the slug, which iCIMS ignores.
    assert p.url == "https://careers-gdms.icims.com/jobs/1/job"
    assert p.company == "General Dynamics Mission Systems"
    assert p.locations == ["US-MD-Annapolis Junction"]
    assert p.date_posted == int(datetime.datetime(2026, 10, 4, tzinfo=datetime.UTC).timestamp())
    # The card's snippet is not the posting's text; the page fetch supplies it.
    assert p.description == ""
    assert p.raw is not None and p.raw["Required Clearance"] == "TS/SCI"


def test_icims_reads_header_locations_split_on_the_bar(monkeypatch):
    _serve_icims(monkeypatch, _JOBY_CARD, {0: [4815]}, pages=1)
    (p,) = boards.fetch_listings("https://careers-jobyaviation.icims.com/jobs/search", "Joby")
    assert p.locations == ["US-CA-Marina", "US-CA-Watsonville"]
    assert p.date_posted == 0


def test_icims_a_page_that_shifted_under_the_pull_makes_it_partial(monkeypatch):
    """A posting added mid-pull pushes the last card of page one onto page
    two: every page is full and the page count holds, but one posting repeats
    and another was never served. Returning the deduplicated list would retire
    the unseen one as closed."""
    _serve_icims(monkeypatch, _GDMS_CARD, {0: [1, 2], 1: [2, 3], 2: [4]}, pages=3)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://careers-gdms.icims.com/jobs/search", "GDMS")
    assert sorted(p.title for p in raised.value.postings) == [
        "Role 1",
        "Role 2",
        "Role 3",
        "Role 4",
    ]


def test_icims_a_page_cut_short_before_the_last_makes_it_partial(monkeypatch):
    _serve_icims(monkeypatch, _GDMS_CARD, {0: [1, 2], 1: [3], 2: [4]}, pages=3)
    with pytest.raises(boards.PartialPull):
        boards.fetch_listings("https://careers-gdms.icims.com/jobs/search", "GDMS")


def test_an_icims_portal_that_moved_names_its_career_site(monkeypatch):
    # careers-spiritaero.icims.com/jobs/search?ss=1&in_iframe=1, 2026-10-05,
    # verbatim: the whole 200 body.
    body = (
        '<script type="text/javascript">\n'
        "window.top.location.href = 'https:\\/\\/careers.spiritaero.com\\/jobs';\n"
        "</script>\n"
    )
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _Resp(body))
    with pytest.raises(ValueError, match=r"https://careers\.spiritaero\.com/api/jobs"):
        boards.fetch_listings("https://careers-spiritaero.icims.com/jobs/search", "Spirit")
