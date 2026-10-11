"""Resolve Ashby posting detail and availability."""

from __future__ import annotations

import re
from urllib.parse import unquote

from core.fetching.ats_common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    ashby_text,
)


class Ashby(AtsResolver):
    name = "ashby"
    markers = ("ashbyhq.com",)

    def canonical(self, url: str) -> str | None:
        match = re.search(r"ashbyhq\.com/([^/?]+)/([0-9a-f-]{36})", url)
        return f"https://jobs.ashbyhq.com/{match.group(1)}/{match.group(2)}" if match else None

    def fetch(self, url: str) -> AtsResult:
        match = re.search(r"ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
        if not match:
            return UNSUPPORTED
        org, job_id = unquote(match.group(1)), match.group(2)
        resp = self.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        for job in resp.json().get("jobs", []):
            if job.get("id") == job_id or job_id in str(job.get("jobUrl", "")):
                return self.result(ashby_text(job))
        return AtsResult(Status.GONE, source=self.name)
