"""core.fetching.ats: the ADP Workforce Now resolver, over detail bodies
copied from the live API on 2026-11-01 (PathStone) and trimmed to the keys
read."""

from __future__ import annotations

import datetime

from core.fetching import ats
from tests.fetching.ats.responses import AtsResponse

_CID = "89066319-3d2d-43f2-88e4-ea45aa701c73"
_PAGE = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
_DETAIL = (
    "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
    "job-requisitions/604069"
)
_POSTING = f"{_PAGE}?cid={_CID}&jobId=604069"


class _Resp(AtsResponse):
    def __init__(self, body=None, status_code=200, text=""):
        super().__init__(body, status_code)
        self.text = text


def test_adp_canonical_keeps_the_tenant_and_the_job():
    for url in (
        _POSTING,
        f"{_PAGE}?cid={_CID.upper()}&ccId=19000101_000001&jobId=604069&lang=en_US&source=CC2",
        f"{_PAGE}?ccId=19000101_000001&jobId=604069&cid={_CID}",
    ):
        assert ats.canonicalize(url) == _POSTING
    assert ats.canonicalize(f"{_PAGE}?jobId=560273") is None
    assert ats.canonicalize(f"{_PAGE}?cid={_CID}&ccId=19000101_000001&lang=en_US") is None


def test_adp_reads_the_detail_call(monkeypatch):
    asked = []

    def get(url, **kw):
        asked.append((url, kw))
        return _Resp(
            {
                "itemID": "9201250388737_1",
                "postDate": "2026-10-30T18:29:00",
                "requisitionTitle": "Regional Manager",
                "requisitionDescription": "<div><p><strong>Position Summary</strong></p>"
                "<p>Oversee property operations.</p></div>",
            }
        )

    res = ats.Adp()
    monkeypatch.setattr(res, "get", get)
    out = res.fetch(f"{_POSTING}&lang=en_US")
    assert out.ok and out.posted == datetime.date(2026, 10, 30)
    assert out.text == "Regional Manager\n\nPosition Summary\nOversee property operations."
    assert asked == [(_DETAIL, {"params": {"cid": _CID}})]


def test_adp_gone_only_on_the_empty_requisition(monkeypatch):
    res = ats.Adp()
    empty = {"postingInstructions": [], "links": [], "requisitionLocations": []}
    for resp, want in (
        (_Resp(empty), ats.Status.GONE),
        (_Resp(None, 404, "<html><center>openresty</center></html>"), ats.Status.ERROR),
        (_Resp(None, 503), ats.Status.ERROR),
        (None, ats.Status.ERROR),
    ):
        monkeypatch.setattr(res, "get", lambda url, resp=resp, **kw: resp)
        assert res.fetch(_POSTING).status is want


def test_adp_asks_another_node_past_a_refusal(monkeypatch):
    answers = [
        _Resp(None, 404, "default backend - 404"),
        _Resp(
            {
                "itemID": "1",
                "requisitionTitle": "Analyst",
                "requisitionDescription": "<p>Do analysis.</p>",
            }
        ),
    ]
    res = ats.Adp()
    monkeypatch.setattr(res, "get", lambda url, **kw: answers.pop(0))
    assert res.fetch(_POSTING).ok
