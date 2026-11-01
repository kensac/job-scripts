"""BrassRing board behavior over producer-shaped search excerpts."""

from __future__ import annotations

import html
import json

import pytest

from core.fetching import boards

# General Atomics' search page as it answered on 2026-11-01, cut to the input
# that carries the tenant's display settings. It shows formtext8 as the title
# and names its place fields in LocationCustomSolrFields.
_GA_SETTINGS = {
    "TotalCount": "858",
    "KeywordCustomSolrFields": "Department,FORMTEXT1,FORMTEXT8,FORMTEXT34",
    "LocationCustomSolrFields": "FORMTEXT4,FORMTEXT5",
    "JobFieldsToDisplay": {
        "Position1": "autoreq",
        "JobTitle": "formtext8",
        "Position3": ["department", "formtext5", "formtext4"],
        "Summary": "jobdescription",
    },
}
# Edward Jones names no place field.
_EJ_SETTINGS = {
    "LocationCustomSolrFields": None,
    "JobFieldsToDisplay": {
        "Position1": "LastUpdated",
        "JobTitle": "jobtitle",
        "Position3": ["formtext61", "formtext42", "autoreq"],
        "Summary": "jobdescription",
    },
}
_GA = "https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=25539&siteid=5313"
_SEARCH = "https://sjobs.brassring.com/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"


def _page(settings) -> str:
    value = html.escape(json.dumps(settings))
    return (
        '<html><body><input id="partnerId" type="hidden" value="25539" />'
        f'<input id="searchResults" type="hidden" value="{value}" /></body></html>'
    )


def _row(job_id: int, title: str = "Electronics Test Technician") -> dict:
    """A row as the search returns it, its questions verbatim from General
    Atomics' first row on 2026-11-01 and the description cut short."""
    answers = {
        "reqid": str(job_id),
        "hotjob": "No",
        "clientid": "25539",
        "siteid": "5313",
        "lastupdated": "30-Oct-2026",
        "jobtitle": "Technician I",
        "department": "General Atomics Aeronautical Systems ",
        "jobdescription": "General Atomics Aeronautical Systems, Inc. (GA-ASI), an affiliate",
        "autoreq": "58685BR",
        "formtext4": "California ",
        "formtext5": "Poway",
        "formtext8": title,
    }
    return {
        "Applied": False,
        "IsActive": False,
        "Link": "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25539"
        f"&siteid=5313&PageType=JobDetails&jobid={job_id}",
        "Questions": [
            {"QId": 1, "QuestionName": k, "Value": v, "ActualValueFromSolar": None}
            for k, v in answers.items()
        ],
    }


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise boards.requests.HTTPError(str(self.status_code))

    @property
    def text(self):
        return self.body

    def json(self):
        return json.loads(self.body) if isinstance(self.body, str) else self.body


def _serve(monkeypatch, pages, settings=_GA_SETTINGS, count=None):
    """The search page, then the search as it behaves live: 50 rows a page,
    each stating the count, and an empty page past the last."""
    asked: list[dict] = []
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _Resp(_page(settings)))

    def post(url, json=None, **kw):
        assert url == _SEARCH
        asked.append(json)
        n = json["pageNumber"]
        rows = pages[n - 1] if n <= len(pages) else []
        total = count if count is not None else sum(len(p) for p in pages)
        return _Resp({"JobsCount": total, "Jobs": {"Job": rows}, "PageSize": 0})

    monkeypatch.setattr(boards._session, "post", post)
    return asked


def test_brassring_reads_every_page_with_the_tenants_own_title_and_place(monkeypatch):
    rows = [_row(5112468 + n) for n in range(120)]
    asked = _serve(monkeypatch, [rows[:50], rows[50:100], rows[100:]])
    postings = boards.fetch_listings(_GA, "General Atomics")
    # Pages by number to the page the count implies, sorted by the
    # requisition key so tied dates cannot trade rows between pages.
    assert [a["pageNumber"] for a in asked] == [1, 2, 3]
    assert {a["SortType"] for a in asked} == {"reqid"}
    assert {(a["partnerId"], a["siteId"]) for a in asked} == {("25539", "5313")}
    assert len(postings) == 120
    first = postings[0]
    assert first.company == "General Atomics"
    assert first.title == "Electronics Test Technician"
    assert first.locations == ["California, Poway"]
    assert first.url == (
        "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25539"
        "&siteid=5313&PageType=JobDetails&jobid=5112468"
    )
    # The summary is one section of the posting, not its text.
    assert first.description == ""
    assert "jobdescription" not in first.raw
    assert first.raw["autoreq"] == "58685BR"


def test_a_tenant_that_names_no_place_field_lists_no_place(monkeypatch):
    _serve(monkeypatch, [[_row(729475)]], settings=_EJ_SETTINGS)
    [posting] = boards.fetch_listings(_GA, "Edward Jones")
    assert posting.title == "Technician I"
    assert posting.locations == []


def test_an_empty_brassring_board_asks_once_and_holds_nothing(monkeypatch):
    # Lockheed Martin's site 5014 stated 0 on 2026-11-01.
    asked = _serve(monkeypatch, [])
    assert boards.fetch_listings(_GA, "Lockheed Martin") == []
    assert len(asked) == 1


def test_a_brassring_walk_short_of_its_count_is_partial(monkeypatch):
    # A row served twice means another was never served.
    rows = [_row(5112468 + n) for n in range(60)]
    _serve(monkeypatch, [rows[:50], rows[:10]], count=60)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(_GA, "General Atomics")
    assert len(raised.value.postings) == 50


def test_a_page_without_the_settings_is_not_an_empty_board(monkeypatch):
    # A retired site (partner 25515) answers a page with no settings.
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _Resp("<html></html>"))
    with pytest.raises(ValueError, match="not a BrassRing search page"):
        boards.fetch_listings(_GA, "General Atomics")


def test_a_search_that_answers_an_error_page_fails_the_pull(monkeypatch):
    # An unknown partner answers 500 with an HTML error page.
    monkeypatch.setattr(boards._session, "get", lambda url, **kw: _Resp(_page(_GA_SETTINGS)))
    monkeypatch.setattr(
        boards._session, "post", lambda url, **kw: _Resp("<!DOCTYPE html><h1>Error</h1>", 500)
    )
    with pytest.raises(boards.requests.HTTPError):
        boards.fetch_listings(_GA, "General Atomics")


def test_a_brassring_url_without_its_site_is_refused(monkeypatch):
    with pytest.raises(ValueError, match="partnerid and a siteid"):
        boards.fetch_listings(
            "https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=25539", "GA"
        )
