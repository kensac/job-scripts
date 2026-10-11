"""Smartrecruiters board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse as _Resp


def test_smartrecruiters_pages_by_offset_and_names_the_company(monkeypatch):
    asked = []

    def get(url, **kw):
        asked.append(url)
        offset = int(url.split("offset=")[1])
        content = {
            0: [
                {
                    "id": "744000147613789",
                    "name": "Software Engineer",
                    "company": {"identifier": "BoschGroup", "name": "Bosch Group"},
                    "releasedDate": "2026-09-05T00:02:29.676Z",
                    "location": {
                        "city": "Guadalajara",
                        "region": "Jal.",
                        "country": "mx",
                        "fullLocation": "Guadalajara, Jal., Mexico",
                    },
                },
            ],
            1: [
                {
                    "id": "87619425",
                    "name": "Software Developer- Full Stack",
                    "company": {"identifier": "Zoro", "name": "Zoro"},
                    "releasedDate": "2015-12-03T15:03:39.000Z",
                    "location": {"city": "Buffalo Grove", "region": "IL", "country": "us"},
                },
            ],
        }[offset]
        return _Resp({"offset": offset, "limit": 100, "totalFound": 2, "content": content})

    monkeypatch.setattr(boards._session, "get", get)
    out = boards.fetch_listings("https://api.smartrecruiters.com/v1/companies/BoschGroup/postings")
    assert [(p.company, p.title, p.locations) for p in out] == [
        ("Bosch Group", "Software Engineer", ["Guadalajara, Jal., Mexico"]),
        ("Zoro", "Software Developer- Full Stack", ["Buffalo Grove, IL, us"]),
    ]
    assert out[0].url == "https://jobs.smartrecruiters.com/BoschGroup/744000147613789"
    assert out[0].date_posted == 1788566549
    assert [u.split("?")[1] for u in asked] == ["limit=100&offset=0", "limit=100&offset=1"]
