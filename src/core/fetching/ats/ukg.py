"""Resolve UKG Pro Recruiting (UltiPro) posting detail and availability."""

from __future__ import annotations

import json
import re
from dataclasses import replace

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    parse_posted_date,
    ukg_text,
)

_DETAIL = "CandidateOpportunityDetail("


class Ukg(AtsResolver):
    """The posting page embeds the opportunity as JSON in a script.

    The board's search carries a short summary only, so the text is read
    here. An open posting's object says `OpportunityIsClosed: false`. A
    posting the board no longer lists and an id the board never had both
    answer 200 with no object at all (Ascend Learning's retired board and a
    made-up id on Partner's, 2026-11-01), so a missing object is ERROR,
    never GONE: the board pull is what closes these postings.
    """

    name = "ukg"
    # The slash keeps the markers out of the mail domains, which inherit
    # resolver markers.
    markers = ("ultipro.com/", "ultipro.ca/")
    _JOB = re.compile(
        r"(recruiting\d*\.ultipro\.(?:com|ca))/([^/]+)/JobBoard/([0-9a-fA-F-]{36})"
        r"/OpportunityDetail\?(?:[^#]*&)?opportunityId=([0-9a-fA-F-]{36})"
    )

    def canonical(self, url: str) -> str | None:
        match = self._JOB.search(url)
        if not match:
            return None
        host, tenant, board, job = match.groups()
        return f"https://{host}/{tenant}/JobBoard/{board}/OpportunityDetail?opportunityId={job}"

    def fetch(self, url: str) -> AtsResult:
        canonical = self.canonical(url)
        if canonical is None:
            return UNSUPPORTED
        resp = self.get(canonical, headers={"Accept": "text/html"})
        if resp is None or resp.status_code != 200:
            return AtsResult(Status.ERROR, source=self.name)
        start = resp.text.find(_DETAIL)
        if start < 0:
            return AtsResult(Status.ERROR, source=self.name)
        try:
            data, _ = json.JSONDecoder().raw_decode(resp.text, start + len(_DETAIL))
        except ValueError:
            return AtsResult(Status.ERROR, source=self.name)
        if data.get("OpportunityIsClosed"):
            return AtsResult(Status.GONE, source=self.name)
        return replace(
            self.result(ukg_text(data)), posted=parse_posted_date(data.get("PostedDate"))
        )
