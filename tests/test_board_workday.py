"""Workday board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import pytest

from core.fetching import boards
from tests.board_responses import BoardResponse as _Resp


def test_workday_pages_until_the_first_pages_total_and_builds_the_public_url(monkeypatch):
    posts = []

    def post(url, json, **kw):
        posts.append((url, json))
        page = {
            0: [
                {
                    "externalPath": "/job/USA---NAS-JRB-New-Orleans-LA/F-18-General-Mechanic_JR2026517674-2",
                    "locationsText": "USA - NAS JRB New Orleans, LA",
                    "postedOn": "Posted 14 Days Ago",
                    "title": "F-18 General Mechanic",
                },
                {
                    "externalPath": "/job/x/Associate-Software-Engineer_JR1",
                    "locationsText": "Seattle, WA",
                    "postedOn": "Posted 30+ Days Ago",
                    "title": "Associate Software Engineer",
                },
            ],
            2: [
                {
                    "externalPath": "/job/y/Software-Engineer_JR2",
                    "locationsText": "Everett, WA",
                    "postedOn": "Posted Today",
                    "title": "Software Engineer",
                }
            ],
            3: [
                {
                    "externalPath": "/job/z/Structures-Engineer_JR3",
                    "locationsText": "Seattle, WA",
                    "postedOn": "Posted Today",
                    "title": "Structures Engineer",
                }
            ],
        }[json["offset"]]
        # As the live API does: the count rides on the first page only, and
        # every later page says 0 (Boeing, 2026-10-05: 752, then 0, then 0).
        return _Resp({"total": 4 if json["offset"] == 0 else 0, "jobPostings": page})

    monkeypatch.setattr(boards._session, "post", post)
    out = boards.fetch_listings(
        "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/jobs?searchText=new+grad",
        "Boeing",
    )
    assert [p.title for p in out] == [
        "F-18 General Mechanic",
        "Associate Software Engineer",
        "Software Engineer",
        "Structures Engineer",
    ]
    assert out[0].url == (
        "https://boeing.wd1.myworkdayjobs.com/EXTERNAL_CAREERS/job/USA---NAS-JRB-New-Orleans-LA/"
        "F-18-General-Mechanic_JR2026517674-2"
    )
    assert out[1].date_posted == 0
    assert all(p.company == "Boeing" for p in out)
    # The search rode in the body, not the URL, and the second page started
    # where the first ended.
    assert [u for u, _ in posts] == [
        "https://boeing.wd1.myworkdayjobs.com/wday/cxs/boeing/EXTERNAL_CAREERS/jobs"
    ] * 3
    assert [(j["offset"], j["searchText"]) for _, j in posts] == [
        (0, "new grad"),
        (2, "new grad"),
        (3, "new grad"),
    ]


def test_a_workday_tenant_capped_at_the_window_is_read_in_facet_slices(monkeypatch):
    """Airbus on 2026-10-05: total=2000 on the first page and pages past offset
    2,000 wrap to the first. Slices by the two widest facets whose values are
    each under the window recover the rest, and the pull says it is partial."""
    facets = [
        {
            "facetParameter": "jobFamilyGroup",
            "values": [{"id": "eng", "count": 3}, {"id": "ops", "count": 1}],
        },
        {
            "facetParameter": "locationMainGroup",
            "values": [
                {
                    "facetParameter": "locationCountry",
                    "values": [{"id": "fr", "count": 2}, {"id": "us", "count": 2}],
                }
            ],
        },
        {"facetParameter": "FullPartTime", "values": [{"id": "ft", "count": 2500}]},
    ]
    by_slice = {
        (): ["a", "b"],
        (("jobFamilyGroup", ("eng",)),): ["a", "c", "d"],
        (("jobFamilyGroup", ("ops",)),): ["e"],
        (("locationCountry", ("fr",)),): ["a", "f"],
        (("locationCountry", ("us",)),): ["c", "e"],
    }
    asked = []

    def post(url, json, **kw):
        key = tuple(sorted((k, tuple(v)) for k, v in json["appliedFacets"].items()))
        asked.append(key)
        titles = by_slice[key][json["offset"] : json["offset"] + boards._WORKDAY_PAGE]
        page = [{"title": t, "externalPath": f"/job/x/{t}_JR"} for t in titles]
        first = json["offset"] == 0
        total = boards._WORKDAY_WINDOW if not key else len(by_slice[key])
        return _Resp(
            {"total": total if first else 0, "jobPostings": page, "facets": facets if first else []}
        )

    monkeypatch.setattr(boards._session, "post", post)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://ag.wd3.myworkdayjobs.com/wday/cxs/ag/Airbus/jobs", "Airbus")
    assert sorted(p.title for p in raised.value.postings) == ["a", "b", "c", "d", "e", "f"]
    assert ("FullPartTime", ("ft",)) not in {k for key in asked for k in key}, (
        "a facet with a value at the window cannot be read whole"
    )


def test_a_workday_tenant_under_the_window_is_one_complete_pull(monkeypatch):
    def post(url, json, **kw):
        return _Resp(
            {
                "total": 1 if json["offset"] == 0 else 0,
                "facets": [],
                "jobPostings": [{"title": "t", "externalPath": "/job/x/t_JR"}]
                if json["offset"] == 0
                else [],
            }
        )

    monkeypatch.setattr(boards._session, "post", post)
    out = boards.fetch_listings("https://x.wd1.myworkdayjobs.com/wday/cxs/x/Ext/jobs", "X")
    assert [p.title for p in out] == ["t"]
