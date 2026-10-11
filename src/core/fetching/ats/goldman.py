"""Resolve Goldman posting detail and availability."""

from __future__ import annotations

import re
from dataclasses import replace

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    goldman_text,
    parse_posted_date,
)


class Goldman(AtsResolver):
    """A higher.gs.com role, read from the careers site's own GraphQL `role`.

    The text is goldman_text over the same fields the listing fetcher reads,
    so a re-check sees the text the listing stored. The page itself is
    server-rendered and the static tier reads it too (7 of 7 listed roles on
    2026-10-05), with the site's navigation and a firm-wide benefits block
    around the description.

    A role the board no longer lists has no closure signal: 44 of 44 such
    roles from an aggregator's history answered INTERNAL_ERROR, "An exception
    occurred when making a request to an external service", exactly as a
    made-up id and an upstream failure do, and their pages read "Oops,
    something went wrong". So any error is ERROR and never GONE, the page
    tiers get their turn, and the board's authoritative pull closes the role.
    """

    name = "goldman"
    # The path keeps the marker out of the mail domains, which inherit
    # resolver markers: whether higher.gs.com sends application mail has not
    # been measured.
    markers = ("higher.gs.com/roles/",)
    _ROLE = re.compile(r"higher\.gs\.com/roles/(\d+)")
    _QUERY = """query Role($id: String!) {
  role(externalSourceId: $id) {
    jobTitle corporateTitle status externalJobStatus lastPostedDate
    locations { city state country }
    compensation { minSalary maxSalary currency }
    descriptionHtml
  }
}"""

    def fetch(self, url: str) -> AtsResult:
        match = self._ROLE.search(url)
        if not match:
            return UNSUPPORTED
        resp = self.post(
            "https://api-higher.gs.com/gateway/api/v1/graphql",
            json={"query": self._QUERY, "variables": {"id": match.group(1)}},
        )
        # Not from_response: the gateway answers a missing role 200 with
        # INTERNAL_ERROR (a made-up id, 2026-10-10) and a wrong path 401, so a
        # status here is about the endpoint, never the role, and a 404 read as
        # GONE would close every Goldman posting the day the path moved.
        if resp is None or resp.status_code != 200:
            return AtsResult(Status.ERROR, source=self.name)
        role = (resp.json().get("data") or {}).get("role") or {}
        # Every role listed on 2026-10-05 read POSTED. The schema also has
        # UNPOSTED, EXPIRED and SCHEDULED, none of them seen, so their text is
        # not offered as a live posting's.
        if role.get("externalJobStatus") not in (None, "POSTED") or not role.get("descriptionHtml"):
            return AtsResult(Status.ERROR, source=self.name)
        return replace(
            self.result(goldman_text(role)), posted=parse_posted_date(role.get("lastPostedDate"))
        )
