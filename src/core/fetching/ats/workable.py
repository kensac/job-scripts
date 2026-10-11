"""Resolve Workable posting detail and availability."""

from __future__ import annotations

import re
from dataclasses import replace

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    parse_posted_date,
    workable_text,
)


class Workable(AtsResolver):
    name = "workable"
    markers = ("workable.com",)
    _JOB = re.compile(r"workable\.com/([^/]+)/j/([A-Za-z0-9]+)")

    def canonical(self, url: str) -> str | None:
        match = self._JOB.search(url)
        return (
            f"https://apply.workable.com/{match.group(1)}/j/{match.group(2).upper()}/"
            if match
            else None
        )

    def fetch(self, url: str) -> AtsResult:
        match = self._JOB.search(url)
        if not match:
            return UNSUPPORTED
        resp = self.get(
            f"https://apply.workable.com/api/v2/accounts/{match.group(1)}/jobs/{match.group(2)}"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        data = resp.json()
        return replace(
            self.result(workable_text(data)), posted=parse_posted_date(data.get("published"))
        )
