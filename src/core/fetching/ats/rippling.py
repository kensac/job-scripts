"""Resolve Rippling posting detail and availability."""

from __future__ import annotations

import re
from dataclasses import replace

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    parse_posted_date,
    rippling_text,
)


class Rippling(AtsResolver):
    """A board's list carries no text, so its postings read it here.

    A posting the board stopped listing answers 404 with `{"ok": false}`
    (20 of 20, and 9 of 9 listed ones answered 200, 2026-10-16). A board
    that does not exist answers 404 with `RESOURCE_NOT_FOUND`, which is the
    url's board being wrong, not the posting closing.
    """

    name = "rippling"
    markers = ("ats.rippling.com",)
    # A locale (en-GB) or embed/ prefix is the same posting.
    _JOB = re.compile(
        r"ats\.rippling\.com/(?:(?:embed|[a-z]{2}-[A-Z]{2})/)*([^/?#]+)/jobs/([0-9a-f-]{36})"
    )

    def canonical(self, url: str) -> str | None:
        match = self._JOB.search(url)
        return f"https://ats.rippling.com/{match.group(1)}/jobs/{match.group(2)}" if match else None

    def fetch(self, url: str) -> AtsResult:
        match = self._JOB.search(url)
        if not match:
            return UNSUPPORTED
        resp = self.get(
            f"https://ats.rippling.com/api/v2/board/{match.group(1)}/jobs/{match.group(2)}"
        )
        if resp is None:
            return AtsResult(Status.ERROR, source=self.name)
        if resp.status_code == 404:
            try:
                closed = resp.json().get("ok") is False
            except ValueError:
                closed = False
            return AtsResult(Status.GONE if closed else Status.ERROR, source=self.name)
        if resp.status_code != 200:
            return AtsResult(Status.ERROR, source=self.name)
        data = resp.json()
        return replace(
            self.result(rippling_text(data)), posted=parse_posted_date(data.get("createdOn"))
        )
