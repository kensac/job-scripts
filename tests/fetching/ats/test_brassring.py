"""core.fetching.ats: the BrassRing resolver, over the preLoadJSON input of
jobs.ubs.com's page for jobid 350625 on 2026-11-01, trimmed to a few fields."""

from __future__ import annotations

import html
import json

from core.fetching import ats
from core.fetching.ats.brassring import BrassRing


def _q(label, zone, value, shown=True):
    return {
        "QuestionName": label,
        "AnswerValue": value,
        "VerityZone": zone,
        "ClassName": "section2LeftfieldsInJobDetails" if shown else None,
    }


_DETAILS = {
    "Title": None,
    "JobDetailQuestions": [
        _q("", "lastupdated", "29-Oct-2026"),
        _q("", "jobtitle", "Associate Program - Client Advisor Wealth Management team WM Türkiye"),
        _q("", "formtext23", "Switzerland - Zürich "),
        _q("City", "formtext2", "Zürich "),
        _q(
            "Key responsibilities",
            "jobdescription",
            "Are you a true networker?<br>Can you also close a deal?",
        ),
        _q("Your skills and experience", "formtext59", "• a Bachelor or Master"),
        _q("", "reqid", "350625", shown=False),
        _q("", "latitude", "0.0", shown=False),
    ],
}
_URL = (
    "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25008&siteid=5012"
    "&PageType=JobDetails&jobid=350625"
)


def _page(details) -> str:
    value = html.escape(json.dumps({"SmartSearchJSONValue": None, "Jobdetails": details}))
    return (
        "<html><head><title>Job Details</title></head><body>"
        f'<input id="preLoadJSON" type="hidden" value="{value}" '
        'capture-escaped-parsed-value="preloadResponse" /></body></html>'
    )


class _Resp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _serve(monkeypatch, resp: _Resp) -> list[str]:
    asked: list[str] = []
    monkeypatch.setattr(ats._session, "get", lambda url, **kw: asked.append(url) or resp)
    return asked


def test_brassring_reads_the_posting_from_the_json_its_page_carries(monkeypatch):
    asked = _serve(monkeypatch, _Resp(_page(_DETAILS)))
    res = ats.resolve(_URL.replace("jobid=", "codes=ILNK&jobId=").replace("partnerid", "PartnerId"))
    assert asked == [_URL]
    assert res.ok and res.source == "brassring"
    assert res.text == (
        "29-Oct-2026\n\n"
        "Associate Program - Client Advisor Wealth Management team WM Türkiye\n\n"
        "Switzerland - Zürich\n\n"
        "City\n\nZürich\n\n"
        "Key responsibilities\n\nAre you a true networker?\nCan you also close a deal?\n\n"
        "Your skills and experience\n\n• a Bachelor or Master"
    )


def test_a_brassring_page_without_its_posting_is_an_error_never_gone(monkeypatch):
    # A delisted id and a made-up one both answer 200 with Jobdetails null.
    _serve(monkeypatch, _Resp(_page(None)))
    res = ats.resolve(_URL)
    assert res.status is ats.Status.ERROR and res.text is None
    _serve(monkeypatch, _Resp("<html>Not Found</html>", 404))
    assert ats.resolve(_URL).status is ats.Status.ERROR


def test_brassring_collapses_a_postings_spellings_onto_one_url():
    assert ats.canonicalize(
        "https://SJOBS.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?PageType=JobDetails"
        "&partnerid=25539&siteid=5313&jobId=5033838&codes=ILNK&dclid=CPmK"
    ) == (
        "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25539"
        "&siteid=5313&PageType=JobDetails&jobid=5033838"
    )


def test_brassring_leaves_its_search_page_alone(monkeypatch):
    asked = _serve(monkeypatch, _Resp(_page(_DETAILS)))
    search = "https://jobs.ubs.com/TGnewUI/Search/Home/Home?partnerid=25008&siteid=5012"
    assert ats.resolve(search).status is ats.Status.UNSUPPORTED
    no_job = _URL.replace("&jobid=350625", "")
    assert BrassRing().fetch(no_job).status is ats.Status.UNSUPPORTED
    assert asked == []
