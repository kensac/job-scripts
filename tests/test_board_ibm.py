"""Ibm board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import pytest

from core.fetching import boards
from core.fetching.boards import ibm as board_ibm
from tests.board_responses import BoardResponse as _Resp

IBM_HITS = [
    {
        "_id": "0181e51151251524430513cb843db9ee33255526dbc24195bbb2d832f9193640",
        "_source": {
            "title": "Product Management Intern | Infrastructure Growth & Innovation - "
            "Poughkeepsie, NY / Austin, TX - 2027",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=131158",
            "field_text_01": 131158,
            "field_keyword_05": "United States",
            "field_keyword_08": "Product Management",
            "field_keyword_17": "Hybrid",
            "field_keyword_18": "Internship",
            "field_keyword_19": "Multiple Cities",
        },
    },
    {
        "_id": "0630b4c19c3ac0f2d74de9b57566074deaf113cfcae7def7a35ee2e81e0e9533",
        "_source": {
            "title": "Associate Threat Analyst Researcher and Consultant 2027",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=128676",
            "field_text_01": 128676,
            "field_keyword_05": "United States",
            "field_keyword_08": "Consulting",
            "field_keyword_17": "",
            "field_keyword_18": "Entry Level",
            "field_keyword_19": "Austin, US",
        },
    },
    {
        "_id": "0f4831b26e3dd9abad68616071fe498630390e8f5f1090e589c68e94ee552972",
        "_source": {
            "title": "Client Engineering",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=129622",
            "field_text_01": 129622,
            "field_keyword_05": "Switzerland",
            "field_keyword_08": "Sales",
            "field_keyword_17": "Hybrid",
            "field_keyword_18": "Entry Level",
            "field_keyword_19": "Zurich, CH",
        },
    },
]


def _ibm_search(hits, stated, asked):
    """The search as measured: a size above 100 is a 400, and only a
    search_after on _id moves through the index. `from` is ignored here, so a
    walk by it reads the first page again, as the live walk re-read rows."""

    def post(url, json, **kw):
        asked.append({**json})
        if json["size"] > 100:
            raise boards.requests.HTTPError("400 Parameter 'size' has an invalid value")
        cursor = "search_after" in json and json.get("sort") == [{"_id": "asc"}]
        after = json["search_after"][0] if cursor else ""
        page = [{**h, "sort": [h["_id"]]} for h in hits if h["_id"] > after][: json["size"]]
        return _Resp({"hits": {"total": {"value": stated, "relation": "eq"}, "hits": page}})

    return post


def test_ibm_walks_the_index_by_cursor_and_links_the_public_posting(monkeypatch):
    asked = []
    monkeypatch.setattr(board_ibm, "_IBM_PAGE", 1)
    monkeypatch.setattr(boards._session, "post", _ibm_search(IBM_HITS, 3, asked))
    out = boards.fetch_listings("https://www-api.ibm.com/search/api/v2", "IBM")
    assert [(p.title, p.locations) for p in out] == [
        (
            "Product Management Intern | Infrastructure Growth & Innovation - "
            "Poughkeepsie, NY / Austin, TX - 2027",
            # "Multiple Cities" is not a place; the country is.
            ["United States"],
        ),
        ("Associate Threat Analyst Researcher and Consultant 2027", ["Austin, US"]),
        ("Client Engineering", ["Zurich, CH"]),
    ]
    assert out[1].url == "https://careers.ibm.com/careers/JobDetail?jobId=128676"
    assert all(p.company == "IBM" for p in out)
    # The listing carries no complete text, so none is stored as the posting.
    assert [p.description for p in out] == ["", "", ""]
    assert [a.get("search_after") for a in asked] == [
        None,
        [IBM_HITS[0]["_id"]],
        [IBM_HITS[1]["_id"]],
        [IBM_HITS[2]["_id"]],
    ]


def test_an_ibm_cursor_that_ends_short_of_the_stated_count_is_a_partial_pull(monkeypatch):
    monkeypatch.setattr(board_ibm, "_IBM_PAGE", 1)
    monkeypatch.setattr(boards._session, "post", _ibm_search(IBM_HITS, 4, []))
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://www-api.ibm.com/search/api/v2", "IBM")
    assert len(raised.value.postings) == 3


# api-higher.gs.com roleSearch on 2026-10-05: verbatim roles, each with the
# experience it was listed under, the first description trimmed.
