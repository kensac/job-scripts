"""BambooHR board behavior over replies served on 2026-11-01: United Smart
Tech's listing (trimmed to two rows) and Options Technology's empty one."""

from __future__ import annotations

import pytest

from core.fetching import boards
from tests.fetching.boards.responses import BoardResponse

_URL = "https://unitedsmarttech.bamboohr.com/careers/list"

_ROWS = [
    {
        "id": "23",
        "jobOpeningName": "Retail Technician \u2013 Tech Repair & Sales",
        "departmentId": "18561",
        "departmentLabel": "Operations",
        "employmentStatusLabel": "Full-Time",
        "employmentType": None,
        "location": {"city": "Plano", "state": "Texas"},
        "atsLocation": {"country": None, "state": None, "province": None, "city": None},
        "isRemote": None,
        "locationType": "0",
    },
    {
        "id": "101",
        "jobOpeningName": "Store Manager",
        "location": {"city": None, "state": None},
        "atsLocation": {"country": "United States", "state": "Texas", "city": "Frisco"},
    },
]


def _serve(monkeypatch, response):
    asked: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        boards._session, "get", lambda url, **kw: asked.append((url, kw)) or response
    )
    return asked


def test_bamboohr_reads_every_posting_in_one_call(monkeypatch):
    asked = _serve(monkeypatch, BoardResponse({"meta": {"totalCount": 2}, "result": _ROWS}))
    tech, manager = boards.fetch_listings(_URL, "United Smart Tech")
    assert [url for url, _ in asked] == [_URL]
    # A missing tenant redirects to bamboohr.com, so the redirect is not followed.
    assert asked[0][1]["allow_redirects"] is False
    assert boards.kind(_URL) == "bamboohr"
    assert tech.title == "Retail Technician \u2013 Tech Repair & Sales"
    assert tech.url == "https://unitedsmarttech.bamboohr.com/careers/23"
    assert tech.locations == ["Plano, Texas"]
    assert tech.company == "United Smart Tech"
    # The listing has no text; the page tiers read it.
    assert tech.description == ""
    assert manager.locations == ["Frisco, Texas, United States"]


def test_bamboohr_board_with_no_openings_is_empty(monkeypatch):
    _serve(monkeypatch, BoardResponse({"meta": {"totalCount": 0}, "result": []}))
    assert boards.fetch_listings(_URL, "Options Technology") == []


def test_bamboohr_is_partial_when_the_count_exceeds_the_rows(monkeypatch):
    _serve(monkeypatch, BoardResponse({"meta": {"totalCount": 3}, "result": _ROWS}))
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(_URL, "United Smart Tech")
    assert len(raised.value.postings) == 2


def test_bamboohr_unknown_tenant_fails_the_pull(monkeypatch):
    class Redirect(BoardResponse):
        status_code = 302

    _serve(monkeypatch, Redirect(""))
    with pytest.raises(ValueError, match="302"):
        boards.fetch_listings(_URL, "United Smart Tech")


def test_bamboohr_reply_without_rows_fails_the_pull(monkeypatch):
    _serve(monkeypatch, BoardResponse({"meta": {}}))
    with pytest.raises(KeyError):
        boards.fetch_listings(_URL, "United Smart Tech")
