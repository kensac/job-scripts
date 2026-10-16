"""Resolve Gem posting detail."""

from __future__ import annotations

import re
from dataclasses import replace

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    greenhouse_text,
    parse_posted_date,
)


class Gem(AtsResolver):
    """The posting page is a script shell (7 KB, no text), so a re-check
    reads the board API's posting instead.

    Never GONE: its 404 ("Job post with ID ... not found") was measured only
    on made-up ids, never on a posting the board stopped listing (none was
    on record, 2026-10-16). Closure comes from the board's authoritative pull.
    """

    name = "gem"
    markers = ("jobs.gem.com",)
    _JOB = re.compile(r"jobs\.gem\.com/([^/?#]+)/([A-Za-z0-9_-]+)")

    def canonical(self, url: str) -> str | None:
        match = self._JOB.search(url)
        return f"https://jobs.gem.com/{match.group(1)}/{match.group(2)}" if match else None

    def fetch(self, url: str) -> AtsResult:
        match = self._JOB.search(url)
        if not match:
            return UNSUPPORTED
        resp = self.get(
            f"https://api.gem.com/job_board/v0/{match.group(1)}/job_posts/{match.group(2)}"
        )
        if resp is None or resp.status_code != 200:
            return AtsResult(Status.ERROR, source=self.name)
        data = resp.json()
        return replace(
            self.result(greenhouse_text(data)),
            posted=parse_posted_date(data.get("first_published_at")),
        )
