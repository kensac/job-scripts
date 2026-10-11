"""Resolve Workday posting detail and availability."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlparse

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    clean_html,
    join_posting_sections,
    parse_posted_date,
)


class Workday(AtsResolver):
    name = "workday"
    markers = ("myworkdayjobs.com",)

    def canonical(self, url: str) -> str | None:
        parsed = urlparse(url)
        path = re.sub(r"^/[a-z]{2}-[a-z]{2}/", "/", parsed.path, flags=re.IGNORECASE)
        match = re.match(r"^/([^/]+)/job/(.+?)/?$", path)
        return (
            f"https://{parsed.netloc.lower()}/{match.group(1)}/job/{match.group(2)}"
            if match
            else None
        )

    def fetch(self, url: str) -> AtsResult:
        parsed = urlparse(url)
        tenant = parsed.netloc.split(".")[0]
        path = re.sub(r"^/[a-z]{2}-[a-z]{2}/", "/", parsed.path, flags=re.IGNORECASE)
        match = re.match(r"^/([^/]+)/job/(.+)$", path)
        if not match:
            return UNSUPPORTED
        resp = self.get(
            f"https://{parsed.netloc}/wday/cxs/{tenant}/{match.group(1)}/job/{match.group(2)}"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        info = resp.json().get("jobPostingInfo") or {}
        result = self.result(
            join_posting_sections(
                info.get("title"),
                info.get("location"),
                info.get("startDate"),
                clean_html(info.get("jobDescription", "")),
            )
        )
        return replace(result, posted=parse_posted_date(info.get("startDate")))
