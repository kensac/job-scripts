"""Resolve Smartrecruiters posting detail and availability."""

from __future__ import annotations

import re

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    clean_html,
    join_posting_sections,
)


class SmartRecruiters(AtsResolver):
    name = "smartrecruiters"
    markers = ("smartrecruiters.com",)

    _SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")

    def canonical(self, url: str) -> str | None:
        match = re.search(r"smartrecruiters\.com/([^/?]+)/(\d+)", url)
        return (
            f"https://jobs.smartrecruiters.com/{match.group(1)}/{match.group(2)}" if match else None
        )

    def fetch(self, url: str) -> AtsResult:
        match = re.search(r"smartrecruiters\.com/([^/]+)/(\d+)", url)
        if not match:
            return UNSUPPORTED
        resp = self.get(
            f"https://api.smartrecruiters.com/v1/companies/{match.group(1)}/postings/{match.group(2)}"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        data = resp.json()
        # A removed public ad can retain its complete description at HTTP 200.
        # Missing fields are not closure evidence, and truthy strings are not
        # booleans. INTERNAL means unavailable to this public-board audience.
        if data.get("active") is False or data.get("visibility") == "INTERNAL":
            return AtsResult(Status.GONE, source=self.name)
        loc = data.get("location") or {}
        sections = (data.get("jobAd") or {}).get("sections") or {}
        body = [clean_html((sections.get(k) or {}).get("text", "")) for k in self._SECTIONS]
        return self.result(
            join_posting_sections(
                data.get("name"),
                " ".join(str(loc.get(k, "")) for k in ("city", "region", "country")),
                *body,
            )
        )
