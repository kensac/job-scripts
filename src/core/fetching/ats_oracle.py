"""Resolve Oracle posting detail and availability."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlparse

from core.fetching.ats_common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    oracle_text,
    parse_posted_date,
)


class Oracle(AtsResolver):
    """Oracle Recruiting (Fusion HCM) candidate experience: the posting page is
    rendered by script, but the same tenant serves the requisition as JSON."""

    name = "oracle"
    markers = ("oraclecloud.com",)
    # Requisition ids are usually numeric; some tenants prefix them (W737248).
    _JOB = re.compile(r"/sites/([^/]+)/job/([A-Za-z0-9_-]+)")

    def canonical(self, url: str) -> str | None:
        parsed = urlparse(url)
        match = self._JOB.search(parsed.path)
        return (
            f"https://{parsed.netloc.lower()}/hcmUI/CandidateExperience/en/sites/"
            f"{match.group(1)}/job/{match.group(2)}"
            if match
            else None
        )

    def fetch(self, url: str) -> AtsResult:
        parsed = urlparse(url)
        match = self._JOB.search(parsed.path)
        if not match:
            return UNSUPPORTED
        site, job_id = match.groups()
        resp = self.get(
            f"https://{parsed.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?onlyData=true&expand=all&finder=ById;siteNumber={site},Id=%22{job_id}%22"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        items = resp.json().get("items") or []
        # A requisition that is no longer posted comes back as an empty list,
        # not a 404.
        if not items:
            return AtsResult(Status.GONE, source=self.name)
        return replace(
            self.result(oracle_text(items[0])),
            posted=parse_posted_date(
                items[0].get("ExternalPostedStartDate") or items[0].get("PostedDate")
            ),
        )
