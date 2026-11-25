"""core.fetching.ats.Workday: what a posting read answers, over bodies copied
from Workday's API on 2026-11-25."""

from __future__ import annotations

import pytest

from core.fetching import ats
from tests.fetching.ats.responses import AtsResponse

POSTING = (
    "https://advanceauto.wd5.myworkdayjobs.com/advanceexternalcareers/job/"
    "10003-Charlotte-Hwy-Fort-Mill-SC-29707/Store-Driver_R-0603524"
)
# A posting advanceauto took down.
UNPUBLISHED = {
    "errorCode": "S22",
    "errorCaseId": "7765BEMWVTFI64",
    "httpStatus": 403,
    "message": "permission denied",
    "messageParams": {},
}
# An id the tenant never had.
NOT_FOUND = {
    "errorCode": "S21",
    "errorCaseId": "572C15MWVTFIGL",
    "httpStatus": 404,
    "message": "not found: Job_Posting_Anchor_ID=Fake_R-0000001",
    "messageParams": {},
}


class _Html(AtsResponse):
    """A Cloudflare challenge: 403 with a page, not Workday's JSON."""

    def json(self):
        raise ValueError("not json")


def _read(monkeypatch, reply) -> ats.AtsResult:
    wd = ats.Workday()
    monkeypatch.setattr(wd, "get", lambda url: reply)
    return wd.fetch(POSTING)


def test_a_posting_the_tenant_took_down_is_gone(monkeypatch):
    assert _read(monkeypatch, AtsResponse(UNPUBLISHED, 403)).status is ats.Status.GONE


def test_an_id_that_never_existed_is_gone(monkeypatch):
    assert _read(monkeypatch, AtsResponse(NOT_FOUND, 404)).status is ats.Status.GONE


@pytest.mark.parametrize(
    "reply",
    [
        _Html(None, 403),
        AtsResponse({"errorCode": "S99", "message": "something else"}, 403),
        AtsResponse(["not", "an", "object"], 403),
    ],
)
def test_any_other_403_is_not_a_closure(monkeypatch, reply):
    """A blocked address must never close a posting."""
    assert _read(monkeypatch, reply).status is ats.Status.ERROR


def test_a_refusal_is_still_a_refusal(monkeypatch):
    assert _read(monkeypatch, AtsResponse(UNPUBLISHED, 429)).status is ats.Status.REFUSED
