"""Resolve Eightfold detail only with a vouched-for source URL."""

from __future__ import annotations

import datetime
import re
from dataclasses import replace
from urllib.parse import urlparse

from core.fetching.ats_common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    clean_html,
    join_posting_sections,
)
from core.fetching.hosts import EIGHTFOLD_PCSX, EIGHTFOLD_V2


class Eightfold(AtsResolver):
    """An Eightfold posting, read from its tenant's detail endpoint.

    The posting page is a shell. On 2026-10-05 the static tier returned
    139,000 to 163,000 characters for eight Lockheed and Northrop postings,
    the title and the site's theme JSON, and not one 50-character run of the
    description; the gate passed all eight. A removed posting's page is the
    same shell. The detail endpoint carries the text and answers 404 for a
    posting it no longer has.

    A tenant posts on its own domain (jobs.northropgrumman.com), so a posting
    URL alone does not say it is Eightfold's, and a guessed endpoint on a host
    that is not would answer 404 and read as a closure. So this never matches
    a URL by itself: it answers only beside the listings URL of a source on
    the same host (resolve's listings_url), and its markers stay empty, which
    also keeps it out of the mail domains.
    """

    name = "eightfold"
    markers = ()
    _JOB = re.compile(r"^/careers/job/(\d+)/?$")

    def matches(self, url: str) -> bool:
        return False

    def fetch(self, url: str) -> AtsResult:
        return UNSUPPORTED

    def fetch_listed(self, url: str, listings_url: str) -> AtsResult | None:
        """The posting's text, or None when listings_url is not an Eightfold
        listing on the posting's own host."""
        posting, listing = urlparse(url), urlparse(listings_url)
        match = self._JOB.match(posting.path)
        generation = listing.path.rstrip("/")
        if (
            not match
            or posting.netloc.lower() != listing.netloc.lower()
            or generation not in (EIGHTFOLD_PCSX, EIGHTFOLD_V2)
        ):
            return None
        job_id = match.group(1)
        origin = f"https://{listing.netloc}"
        pcsx = generation == EIGHTFOLD_PCSX
        resp = self.get(
            f"{origin}/api/pcsx/position_details?position_id={job_id}"
            if pcsx
            else f"{origin}{EIGHTFOLD_V2}/{job_id}"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        body = resp.json()
        data = (body.get("data") or {}) if pcsx else body
        text = data.get("jobDescription") if pcsx else data.get("job_description")
        result = self.result(
            join_posting_sections(
                data.get("name"),
                "; ".join(data.get("locations") or []),
                clean_html(text or ""),
            )
            if text
            else None
        )
        ts = data.get("postedTs") if pcsx else data.get("t_create")
        posted = datetime.datetime.fromtimestamp(int(ts), datetime.UTC).date() if ts else None
        return replace(result, posted=posted)


EIGHTFOLD = Eightfold()
