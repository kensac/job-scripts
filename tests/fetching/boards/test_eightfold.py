"""Eightfold board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from core.fetching import boards, client
from tests.fetching.boards.responses import BoardResponse as _Resp

NGC_ROW = {
    "id": 1340074304655,
    "displayJobId": "R10250443",
    "name": "U103 PRODUCTION COORD A",
    "locations": ["United States-California-Sunnyvale"],
    "standardizedLocations": ["Sunnyvale, CA, US"],
    "postedTs": 1791158400,
    "solrScore": None,
    "stars": 0,
    "department": "NGC - Non - NGJF Union",
    "creationTs": 1789689600,
    "isHot": 0,
    "workLocationOption": "onsite",
    "locationFlexibility": None,
    "atsJobId": "R10250443",
    "positionUrl": "/careers/job/1340074304655",
}


def _pcsx_board(rows: list[dict], asked: list[str]):
    """The live contract, measured on Northrop, Lockheed and Starbucks on
    2026-10-05: ten rows from `start` whatever `num` says, the count on every
    page, and an empty page (still 200, still the count) past the end."""

    def get(url, **kw):
        asked.append(url)
        start = int(parse_qs(urlparse(url).query)["start"][0])
        return _Resp(
            {
                "status": 200,
                "error": {"message": "", "body": ""},
                "data": {"positions": rows[start : start + 10], "count": len(rows)},
            }
        )

    return get


def _ngc_rows(n: int) -> list[dict]:
    return [
        NGC_ROW | {"id": i, "name": f"Engineer {i}", "positionUrl": f"/careers/job/{i}"}
        for i in range(n)
    ]


def test_eightfold_pages_by_the_ten_rows_it_returns_until_an_empty_page(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(boards._session, "get", _pcsx_board(_ngc_rows(23), asked))
    out = boards.fetch_listings(
        "https://jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com", "Northrop Grumman"
    )
    # Every row, so a loop that advanced by a page size it asked for, or
    # stopped on a short first page, fails here.
    assert [p.title for p in out] == [f"Engineer {i}" for i in range(23)]
    assert [parse_qs(urlparse(u).query)["start"] for u in asked] == [
        ["0"],
        ["10"],
        ["20"],
        ["23"],
    ]
    assert all(parse_qs(urlparse(u).query)["domain"] == ["ngc.com"] for u in asked)
    p = out[0]
    assert p.url == "https://jobs.northropgrumman.com/careers/job/0"
    assert p.company == "Northrop Grumman"
    # The tenant's own place, not standardizedLocations, which Eightfold
    # derives and gets wrong (Lockheed's Dartmouth, Nova Scotia read
    # "Dartmouth, England, GB").
    assert p.locations == ["United States-California-Sunnyvale"]
    assert p.date_posted == 1791158400
    assert p.raw is not None and p.raw["atsJobId"] == "R10250443"


def test_an_eightfold_pull_that_lost_a_posting_to_a_shifting_page_is_partial(monkeypatch):
    """Microsoft, 2026-10-05: one 3-minute pull read 2,300 rows of which 2,256
    were distinct against a stated 2,297. Here the posting at row 12 is
    re-dated to the top after the first page is read: every later row shifts
    down by one, row 9 comes back on the second page and row 12 is never
    seen. The count never changes, so only counting distinct rows catches it."""
    rows = _ngc_rows(15)
    asked: list[str] = []
    board = _pcsx_board(rows, asked)

    def get(url, **kw):
        if len(asked) == 1:
            rows.insert(0, rows.pop(12))
        return board(url, **kw)

    monkeypatch.setattr(boards._session, "get", get)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://x.eightfold.ai/api/pcsx/search?domain=x.com", "X")
    assert sorted(int(p.url.rsplit("/", 1)[1]) for p in raised.value.postings) == [
        i for i in range(15) if i != 12
    ]


def test_eightfold_v2_reads_its_own_shape(monkeypatch):
    # explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com, 2026-10-05.
    row = {
        "id": 790298014263,
        "name": "AI Engineer 6 - AI Foundation & Tooling, Ads Platform",
        "posting_name": "AI Engineer 6 - AI Foundation & Tooling, Ads Platform",
        "location": "Remote, United States",
        "locations": ["Remote, United States"],
        "hot": 1,
        "department": "Data & Insights",
        "business_unit": "Streaming",
        "t_update": 1779148800,
        "t_create": 1721692800,
        "ats_job_id": "AJRT30201",
        "display_job_id": "AJRT30201",
        "type": "ATS",
        "id_locale": "AJRT30201-en-US",
        "job_description": "",
        "locale": "en-US",
        "stars": 0,
        "medallionProgram": None,
        "location_flexibility": None,
        "work_location_option": "onsite",
        "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790298014263",
        "isPrivate": False,
    }

    def get(url, **kw):
        first = parse_qs(urlparse(url).query)["start"] == ["0"]
        return _Resp({"domain": "netflix.com", "positions": [row] if first else [], "count": 1})

    monkeypatch.setattr(boards._session, "get", get)
    (p,) = boards.fetch_listings(
        "https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com", "Netflix"
    )
    assert p.url == "https://explore.jobs.netflix.net/careers/job/790298014263"
    assert (p.title, p.locations, p.date_posted) == (
        "AI Engineer 6 - AI Foundation & Tooling, Ads Platform",
        ["Remote, United States"],
        1721692800,
    )


def test_every_eightfold_tenant_shares_one_pace(monkeypatch):
    """One WAF fronts tenants on different domains, so two tenants' pages wait
    on each other as one host's would."""
    slept = []
    monkeypatch.setattr(client.time, "sleep", lambda s: slept.append(round(s, 1)))
    monkeypatch.setattr(boards._session, "get", _pcsx_board([], []))
    monkeypatch.setattr(client, "_PACE_SECONDS", {})
    monkeypatch.setattr(client, "_last_call", {})
    client.set_pace({"eightfold.ai": 1})
    boards.fetch_listings("https://jobs.northropgrumman.com/api/pcsx/search?domain=ngc.com", "N")
    boards.fetch_listings("https://caci.eightfold.ai/api/pcsx/search?domain=caci.com", "C")
    assert slept and 0.0 < slept[-1] <= 1.0


# --- Avature ----------------------------------------------------------------
#
