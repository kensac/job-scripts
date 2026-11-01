"""core.fetching.ats: the UKG Pro Recruiting resolver, over posting pages
shaped like the live ones on 2026-11-01 and trimmed to the script read."""

from __future__ import annotations

import datetime
import json

from core.fetching import ats
from tests.fetching.ats.responses import AtsResponse

_POSTING = (
    "https://recruiting.ultipro.com/SEA1007SEACO/JobBoard/7851737e-66c7-908f-77e9-6d69f4eb0ead"
    "/OpportunityDetail?opportunityId=28745f89-de05-4f43-ac09-61693ca75353"
)


class _Page(AtsResponse):
    def __init__(self, text, status_code=200):
        super().__init__(None, status_code)
        self.text = text


def _page(opportunity):
    return _Page(
        "<script>var opportunity = new US.Opportunity.CandidateOpportunityDetail("
        + json.dumps(opportunity)
        + ", { jobBoardId: 'x' });</script>"
    )


_OPEN = {
    "Title": "Accounting Manager",
    "Locations": [
        {"Address": {"City": "Stuart", "State": {"Code": "FL"}, "Country": {"Code": "USA"}}}
    ],
    "PostedDate": "2026-10-30T21:34:44.561Z",
    "Description": "<p><strong>ESSENTIAL DUTIES</strong></p><p>Manage close.</p>",
    "OpportunityIsClosed": False,
}


def test_ukg_canonical_drops_tracking_parameters():
    assert ats.canonicalize(f"{_POSTING}&sourceId=9c79851a") == _POSTING
    tracked = _POSTING.replace("?opportunityId", "?sourceId=9c79851a&opportunityId")
    assert ats.canonicalize(tracked) == _POSTING
    assert ats.canonicalize(_POSTING.split("/OpportunityDetail")[0]) is None


def test_ukg_reads_the_embedded_opportunity(monkeypatch):
    asked = []
    res = ats.Ukg()

    def get(url, **kw):
        asked.append(url)
        return _page(_OPEN)

    monkeypatch.setattr(res, "get", get)
    out = res.fetch(f"{_POSTING}&sourceId=x")
    assert out.ok and out.posted == datetime.date(2026, 10, 30)
    assert out.text == "Accounting Manager\n\nStuart, FL, USA\n\nESSENTIAL DUTIES\nManage close."
    assert asked == [_POSTING]


def test_ukg_gone_only_when_the_opportunity_says_closed(monkeypatch):
    res = ats.Ukg()
    for reply, want in (
        (_page({**_OPEN, "OpportunityIsClosed": True}), ats.Status.GONE),
        # A delisted posting and an unknown id both answer the shell alone.
        (_Page("<html>You are using an unsupported browser.</html>"), ats.Status.ERROR),
        (_Page("CandidateOpportunityDetail({broken"), ats.Status.ERROR),
        (_Page("Not Found", 404), ats.Status.ERROR),
        (None, ats.Status.ERROR),
    ):
        monkeypatch.setattr(res, "get", lambda u, r=reply, **kw: r)
        assert res.fetch(_POSTING).status is want
