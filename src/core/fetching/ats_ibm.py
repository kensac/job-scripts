"""Resolve Ibm posting detail and availability."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from core.fetching.ats_common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
)


class Ibm(AtsResolver):
    """A careers.ibm.com posting's closure, read off the Avature host's redirect.

    No unauthenticated endpoint returns the full text. The careers search
    index carries a 256-character snippet and a body without the headings,
    education or years of experience; every careers.ibm.com page and feed
    answers a plain or Chrome-fingerprinted request with an AWS WAF challenge
    (202), so the static tier never gets the page; the browser does (7 of 7
    listed postings on 2026-10-05, about 91% of the index body's 8-word runs
    on the page). So an open posting is UNSUPPORTED here and its text comes
    from the browser.

    ibmglobal.avature.net/en_US/careers/JobDetail?jobId={id} answers without
    a challenge: 301 to careers.ibm.com's JobDetail for an open posting, and
    301 to /careers/Error for one IBM has closed. Measured 2026-10-05: 19 of
    19 ids an aggregator held and the board no longer listed went to Error,
    so did 1 of 10 the search index still listed, and in a browser every one
    of the 9 opened (that one among them) ended on IBM's page "this job is
    closed"; the other 9 listed went to JobDetail. A made-up id goes to Error
    too.
    """

    name = "ibm"
    # The paths keep the markers out of the mail domains; ibmglobal.avature.net
    # is what aggregators link (SimplifyJobs, 19 of 19).
    markers = ("careers.ibm.com/", "ibmglobal.avature.net/")
    _JOB = re.compile(r"(?:careers\.ibm\.com|ibmglobal\.avature\.net)/.*[?&]jobId=(\d+)")

    def fetch(self, url: str) -> AtsResult:
        match = self._JOB.search(url)
        if not match:
            return UNSUPPORTED
        resp = self.get(
            f"https://ibmglobal.avature.net/en_US/careers/JobDetail?jobId={match.group(1)}",
            allow_redirects=False,
        )
        if resp is None:
            return AtsResult(Status.ERROR, source=self.name)
        location = urlparse(resp.headers.get("location") or "").path
        if resp.status_code in (301, 302) and location.endswith("/careers/Error"):
            return AtsResult(Status.GONE, source=self.name)
        return UNSUPPORTED
