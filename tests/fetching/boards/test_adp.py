"""ADP Workforce Now board behavior over replies shaped like the live API on
2026-11-01 (Glenmede, PathStone), trimmed to the keys read."""

from __future__ import annotations

import pytest
import requests

from core.fetching import boards
from core.fetching.boards import adp

_CID = "91a10898-d8ce-4d8a-8e43-34624df15cff"
_URL = (
    "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
    f"job-requisitions?cid={_CID}"
)
_POSTING = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"


class _Resp:
    def __init__(self, body, status_code=200, text=""):
        self.body = body
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


def _row(job_id, title, city, region):
    return {
        "itemID": f"92013005{job_id}_1",
        "customFieldGroup": {
            "stringFields": [
                {"nameCode": {"codeValue": "ExternalJobID"}, "stringValue": job_id},
                {"nameCode": {"codeValue": "HomeDepartment"}, "stringValue": ""},
            ]
        },
        "postDate": "2026-10-31T01:36:00",
        "requisitionLocations": [
            {
                "address": {"cityName": city, "countrySubdivisionLevel1": {"codeValue": region}},
                "nameCode": {"shortName": f" {city}, {region}, US"},
            }
        ],
        "requisitionTitle": title,
    }


_ROWS = [
    _row("559455", "Director of Architecture and Engineering", "Philadelphia", "PA"),
    _row("559102", "GIM, Digital Marketing Associate", "Philadelphia", "PA"),
    _row("558870", "Relationship Management Officer", "Wilmington", "DE"),
]


def _serve(monkeypatch, rows, total, page_size, misses=0):
    """Pages of page_size rows from the one-based $skip, each stating total.
    The first `misses` requests land on nodes that refuse the tenant, the
    bare 404 and the 500 in turn."""
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        if len(asked) <= misses:
            return _Resp(None, 404, "default backend - 404") if len(asked) % 2 else _Resp(None, 500)
        skip = int(url.split("%24skip=")[1].split("&")[0])
        page = rows[skip - 1 : skip - 1 + page_size]
        return _Resp(
            {"jobRequisitions": page, "meta": {"startSequence": skip, "totalNumber": total}}
        )

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_adp_pages_from_one_and_links_the_career_center_page(monkeypatch):
    monkeypatch.setattr(adp, "_ADP_PAGE", 2)
    asked = _serve(monkeypatch, _ROWS, total=3, page_size=2)
    out = boards.fetch_listings(_URL, "Glenmede")
    assert [p.title for p in out] == [r["requisitionTitle"] for r in _ROWS]
    assert out[0].url == f"{_POSTING}?cid={_CID}&jobId=559455"
    assert out[2].locations == ["Wilmington, DE"]
    assert {p.company for p in out} == {"Glenmede"}
    assert out[0].date_posted > 0 and out[0].description == ""
    assert asked == [f"{_URL}&%24top=2&%24skip=1", f"{_URL}&%24top=2&%24skip=3"]


def test_adp_short_of_its_count_is_partial(monkeypatch):
    _serve(monkeypatch, _ROWS[:2], total=3, page_size=20)
    with pytest.raises(boards.PartialPull) as caught:
        boards.fetch_listings(_URL, "Glenmede")
    assert len(caught.value.postings) == 2


def test_adp_empty_board_is_an_empty_list(monkeypatch):
    asked = _serve(monkeypatch, [], total=0, page_size=20)
    assert boards.fetch_listings(_URL, "Glenmede") == []
    assert len(asked) == 1


def test_adp_asks_another_node_without_the_pinning_cookie(monkeypatch):
    asked = _serve(monkeypatch, _ROWS, total=3, page_size=20, misses=2)
    boards._session.cookies.set(
        "workforcenow40g", "3756327435.4395.0000", domain="workforcenow.adp.com"
    )
    assert len(boards.fetch_listings(_URL, "Glenmede")) == 3
    assert len(asked) == 3
    assert "workforcenow40g" not in boards._session.cookies


def test_adp_refused_by_every_node_fails_the_pull(monkeypatch):
    _serve(monkeypatch, _ROWS, total=3, page_size=20, misses=100)
    with pytest.raises(requests.HTTPError):
        boards.fetch_listings(_URL, "Glenmede")


def test_adp_reply_that_is_not_the_list_fails_the_pull(monkeypatch):
    monkeypatch.setattr(
        boards._session, "get", lambda url, **kw: _Resp({"message": "Internal Server Error"})
    )
    with pytest.raises(KeyError):
        boards.fetch_listings(_URL, "Glenmede")
