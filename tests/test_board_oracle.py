"""Oracle board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import pytest

from core.fetching import boards
from tests.board_responses import BoardResponse as _Resp


def test_oracle_reads_the_site_off_the_url_and_pages_by_offset(monkeypatch):
    asked = []

    def get(url, **kw):
        asked.append(url)
        offset = int(url.split("offset=")[1].split(",")[0])
        rows = {
            0: [
                {
                    "Id": "40082",
                    "Title": "Firmware Development Engineer",
                    "PostedDate": "2026-09-05",
                    "PrimaryLocation": "India",
                    "secondaryLocations": [{"Name": "Bangalore, India"}],
                    "ShortDescriptionStr": "Join us.",
                }
            ],
            1: [
                {
                    "Id": "40083",
                    "Title": "Test Engineer",
                    "PostedDate": None,
                    "PrimaryLocation": "Espoo, Finland",
                    "secondaryLocations": [],
                }
            ],
        }[offset]
        return _Resp({"items": [{"TotalJobsCount": 2, "requisitionList": rows}]})

    monkeypatch.setattr(boards._session, "get", get)
    out = boards.fetch_listings(
        "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitions?siteNumber=CX_7",
        "Nokia",
    )
    assert [(p.title, p.locations, p.date_posted) for p in out] == [
        ("Firmware Development Engineer", ["India", "Bangalore, India"], 1788566400),
        ("Test Engineer", ["Espoo, Finland"], 0),
    ]
    assert out[0].url == (
        "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
        "CX_7/job/40082"
    )
    assert all(p.company == "Nokia" for p in out)
    # The finder rides on the URL unencoded, site and offset inside it.
    assert asked[0].endswith(
        "finder=findReqs;siteNumber=CX_7,limit=200,offset=0,sortBy=POSTING_DATES_DESC"
    )
    # The next page starts where the last ended, not at the page size.
    assert "offset=1," in asked[1]


def test_an_oracle_search_past_its_window_is_a_partial_pull(monkeypatch):
    """AutoZone on 2026-10-05: TotalJobsCount 10,788, and at offset 10,000 the
    page is empty and the count reads 0. Returning what arrived as the whole
    board retired the rest on every pull."""

    def get(url, **kw):
        offset = int(url.split("offset=")[1].split(",")[0])
        if offset >= 2:
            return _Resp({"items": [{"TotalJobsCount": 0, "requisitionList": []}]})
        row = {"Id": str(offset), "Title": f"Role {offset}", "PrimaryLocation": "US"}
        return _Resp({"items": [{"TotalJobsCount": 3, "requisitionList": [row]}]})

    monkeypatch.setattr(boards._session, "get", get)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(
            "https://x.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitions?siteNumber=CX_1",
            "AutoZone",
        )
    assert [p.title for p in raised.value.postings] == ["Role 0", "Role 1"]
