"""Goldman board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import pytest

from core.fetching import boards
from core.fetching.boards import goldman as board_goldman
from tests.fetching.boards.responses import BoardResponse as _Resp

GOLDMAN_ROLES = [
    (
        "CAMPUS",
        {
            "roleId": "182810_GS_CAMPUS",
            "jobTitle": "2027 | Americas | New York City Area | Executive Office, "
            "Sustainable Finance Group | Summer Analyst",
            "corporateTitle": "Summer Analyst",
            "jobFunction": "",
            "division": "Executive Office Division",
            "status": "POSTED",
            "lastPostedDate": "2026-10-02T18:48:53.034Z",
            "locations": [
                {"primary": True, "state": "NY", "country": "United States", "city": "New York"}
            ],
            "compensation": {"minSalary": 80000.0, "maxSalary": 110000.0, "currency": "USD"},
            "descriptionHtml": "\n<p><b><u>About the program</u></b></p>\n<p>Our Summer "
            "Analyst Program is a nine to ten week summer internship.</p>",
            "externalSource": {"sourceId": "182810"},
        },
    ),
    (
        "PROFESSIONAL",
        {
            "roleId": "183334_GS_MID_CAREER",
            "jobTitle": "Engineering-L2-Bengaluru- Vice president-Software Engineering",
            "corporateTitle": "Vice President",
            "status": "POSTED",
            "lastPostedDate": "2026-10-01T05:58:03.663Z",
            "locations": [
                {"primary": True, "state": "Karnataka", "country": "India", "city": "Bengaluru"}
            ],
            "compensation": {"minSalary": None, "maxSalary": None, "currency": None},
            "descriptionHtml": None,
            "externalSource": {"sourceId": "183334"},
        },
    ),
    (
        "EARLY_CAREER",
        {
            "roleId": "182221_GS_EARLY_CAREER",
            "jobTitle": "Marcus by Goldman Sachs, Complaints Team Leader, Analyst | Draper, UT",
            "corporateTitle": "Call Center Representative",
            "status": "POSTED",
            "lastPostedDate": "2026-09-29T19:50:16.032Z",
            "locations": [
                {"primary": True, "state": "TX", "country": "United States", "city": "Richardson"},
                {"primary": False, "state": "UT", "country": "United States", "city": "Draper"},
            ],
            "compensation": {"minSalary": None, "maxSalary": None, "currency": "USD"},
            "descriptionHtml": "<p>Lead the complaints team.</p>",
            "externalSource": {"sourceId": "182221"},
        },
    ),
]


def _goldman_search(roles, stated, asked):
    """roleSearch as measured: pages numbered from 0, a pageSize above 250
    refused as HTTP 200 with errors and no data, and only the experiences
    asked for in the result."""

    def post(url, json, **kw):
        query = json["variables"]["searchQueryInput"]
        asked.append(query)
        size, number = query["page"]["pageSize"], query["page"]["pageNumber"]
        if size > 250:
            return _Resp({"errors": [{"message": "Validation Exception."}], "data": None})
        rows = [r for e, r in roles if e in query["experiences"]]
        page = rows[number * size : (number + 1) * size]
        return _Resp({"data": {"roleSearch": {"totalCount": stated, "items": page}}})

    return post


def test_goldman_pages_from_zero_across_every_public_experience(monkeypatch):
    asked = []
    monkeypatch.setattr(board_goldman, "_GOLDMAN_PAGE", 1)
    monkeypatch.setattr(boards._session, "post", _goldman_search(GOLDMAN_ROLES, 3, asked))
    out = boards.fetch_listings("https://api-higher.gs.com/gateway/api/v1/graphql", "Goldman Sachs")
    assert [(p.url, p.locations) for p in out] == [
        ("https://higher.gs.com/roles/182810", ["New York, NY, United States"]),
        ("https://higher.gs.com/roles/183334", ["Bengaluru, Karnataka, India"]),
        (
            "https://higher.gs.com/roles/182221",
            ["Richardson, TX, United States", "Draper, UT, United States"],
        ),
    ]
    assert out[0].date_posted == 1790966933
    assert out[0].description.startswith(
        "2027 | Americas | New York City Area | Executive Office, Sustainable Finance Group | "
        "Summer Analyst\n\nNew York, NY, United States\n\nSummer Analyst\n\n"
        "USD 80,000 - 110,000\n\nAbout the program"
    )
    assert out[0].description.endswith("nine to ten week summer internship.")
    # A role whose listing carries no description stores none, rather than
    # its title and place passing for the posting's text.
    assert out[1].description == ""
    assert out[2].raw is not None and out[2].raw["roleId"] == "182221_GS_EARLY_CAREER"
    assert "descriptionHtml" not in out[2].raw
    assert all(p.company == "Goldman Sachs" for p in out)
    assert [q["page"]["pageNumber"] for q in asked] == [0, 1, 2, 3]


def test_a_refused_goldman_search_fails_the_pull(monkeypatch):
    monkeypatch.setattr(board_goldman, "_GOLDMAN_PAGE", 251)
    monkeypatch.setattr(boards._session, "post", _goldman_search(GOLDMAN_ROLES, 3, []))
    with pytest.raises(RuntimeError, match="roleSearch refused"):
        boards.fetch_listings("https://api-higher.gs.com/gateway/api/v1/graphql", "Goldman Sachs")


def test_a_goldman_pull_short_of_its_count_is_a_partial_pull(monkeypatch):
    monkeypatch.setattr(boards._session, "post", _goldman_search(GOLDMAN_ROLES, 4, []))
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://api-higher.gs.com/gateway/api/v1/graphql", "Goldman Sachs")
    assert len(raised.value.postings) == 3


# www.amazon.jobs/en/search.json, 2026-10-05: one row, its text trimmed.
