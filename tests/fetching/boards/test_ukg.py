"""UKG Pro Recruiting board behavior over replies shaped like the live search
on 2026-11-01 (Seacoast Bank, Salas O'Brien), trimmed to the keys read."""

from __future__ import annotations

import json

import pytest

from core.fetching import boards
from core.fetching.boards import ukg
from tests.fetching.boards.responses import BoardResponse as _Resp

_BOARD = "https://recruiting.ultipro.com/SEA1007SEACO/JobBoard/7851737e-66c7-908f-77e9-6d69f4eb0ead"
_URL = f"{_BOARD}/JobBoardView/LoadSearchResults"


def _place(city, state, description):
    return {
        "LocalizedDescription": description,
        "Address": {
            "City": city,
            "State": {"Code": state, "Name": "Florida"} if state else None,
            "Country": {"Code": "USA", "Name": "United States"},
        },
    }


def _row(n, title, places):
    return {
        "Id": f"28745f89-de05-4f43-ac09-{n:012d}",
        "Title": title,
        "RequisitionNumber": f"ACCOU{n:06d}",
        "FullTime": True,
        "Locations": places,
        "PostedDate": "2026-10-30T21:34:44.561Z",
        "BriefDescription": "Seacoast Bank is seeking an Accounting Manager.",
    }


_ROWS = [
    _row(1, "Accounting Manager", [_place("Stuart", "FL", "Stuart HQ"), _place(None, "FL", "FL")]),
    _row(2, "Credit Analyst", [_place("Tampa", "FL", "Tampa"), _place("Tampa", "FL", "Tampa 2")]),
    _row(3, "Teller", [{"LocalizedDescription": "AZ-Phoenix", "Address": None}]),
]


def _serve(monkeypatch, rows, total):
    """Pages of Top rows from Skip, stating total; past the end, empty and 0."""
    asked: list[dict] = []

    def post(url, json=None, **kw):
        assert url == _URL
        search = json["opportunitySearch"]
        asked.append(search)
        page = rows[search["Skip"] : search["Skip"] + search["Top"]]
        return _Resp({"opportunities": page, "totalCount": total if page else 0})

    monkeypatch.setattr(boards._session, "post", post)
    return asked


def test_ukg_pages_to_the_first_count_and_names_each_posting(monkeypatch):
    monkeypatch.setattr(ukg, "_UKG_PAGE", 2)
    asked = _serve(monkeypatch, _ROWS, total=3)
    out = boards.fetch_listings(_URL, "Seacoast Bank")
    assert [(s["Top"], s["Skip"]) for s in asked] == [(2, 0), (2, 2)]
    assert [p.title for p in out] == ["Accounting Manager", "Credit Analyst", "Teller"]
    assert (
        out[0].url
        == f"{_BOARD}/OpportunityDetail?opportunityId=28745f89-de05-4f43-ac09-000000000001"
    )
    assert out[0].locations == ["Stuart, FL, USA", "FL, USA"]
    # One place listed twice is one place; no address reads the tenant's label.
    assert out[1].locations == ["Tampa, FL, USA"]
    assert out[2].locations == ["AZ-Phoenix"]
    assert out[0].date_posted == 1793396084
    assert {p.company for p in out} == {"Seacoast Bank"}
    # The summary stays in raw; the resolver reads the whole text.
    assert out[0].description == ""


def test_ukg_empty_board_is_an_empty_pull(monkeypatch):
    asked = _serve(monkeypatch, [], total=0)
    assert boards.fetch_listings(_URL, "Seacoast Bank") == []
    assert len(asked) == 1


def test_ukg_short_of_its_count_is_partial(monkeypatch):
    _serve(monkeypatch, _ROWS[:2], total=3)
    with pytest.raises(boards.PartialPull) as caught:
        boards.fetch_listings(_URL, "Seacoast Bank")
    assert len(caught.value.postings) == 2


def test_ukg_reply_without_results_fails_the_pull(monkeypatch):
    # A maintenance page served with a 200 is not an empty board.
    class Html(_Resp):
        def json(self):
            return json.loads(self.body)

    monkeypatch.setattr(boards._session, "post", lambda url, **kw: Html("<!DOCTYPE html>"))
    with pytest.raises(ValueError):
        boards.fetch_listings(_URL, "Seacoast Bank")
    monkeypatch.setattr(boards._session, "post", lambda url, **kw: _Resp({"totalCount": 3}))
    with pytest.raises(KeyError):
        boards.fetch_listings(_URL, "Seacoast Bank")
