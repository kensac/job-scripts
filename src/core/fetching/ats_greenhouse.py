"""Resolve Greenhouse posting detail and availability."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from core.fetching.ats_common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    greenhouse_text,
)


class Greenhouse(AtsResolver):
    name = "greenhouse"
    markers = ("greenhouse.io", "gh_jid=")

    def canonical(self, url: str) -> str | None:
        parsed = urlparse(url)
        match = re.search(r"greenhouse\.io/([^/?]+)/jobs/(\d+)", url)
        if match:
            return f"https://{parsed.netloc.lower()}/{match.group(1)}/jobs/{match.group(2)}"
        job_id = (parse_qs(parsed.query).get("gh_jid") or [None])[0]
        if job_id:
            return f"https://{parsed.netloc.lower()}{parsed.path.rstrip('/')}?gh_jid={job_id}"
        return None

    def fetch(self, url: str) -> AtsResult:
        parsed = urlparse(url)
        board = job_id = None
        match = re.search(r"greenhouse\.io/([^/?]+)/jobs/(\d+)", url)
        if match:
            board, job_id = match.group(1), match.group(2)
        else:
            job_id = (parse_qs(parsed.query).get("gh_jid") or [None])[0]
        if not job_id:
            return UNSUPPORTED

        host = parsed.netloc.replace("www.", "").split(".")[0]
        candidates = [c for c in dict.fromkeys([board, host, host.replace("-", "")]) if c]

        last: AtsResult = AtsResult(Status.ERROR, source=self.name)
        for cand in candidates:
            resp = self.get(
                f"https://boards-api.greenhouse.io/v1/boards/{cand}/jobs/{job_id}?content=true"
            )
            early = self.from_response(resp)
            if early is not None:
                if cand == board:
                    # An explicit board identity owns the answer. Trying a
                    # hostname guess afterwards discarded authoritative 404s.
                    return early
                # A 404 only proves the posting is gone when the board token
                # came explicitly from a greenhouse.io URL. For host-derived
                # guesses (embedded boards on custom domains) a 404 usually
                # means the guess was wrong, not that the job is dead.
                if early.status is Status.GONE and cand != board:
                    last = AtsResult(Status.ERROR, source=self.name)
                else:
                    last = early
                continue
            assert resp is not None
            return self.result(greenhouse_text(resp.json()))
        return last
