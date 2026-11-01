"""Resolve Phenom detail only with a vouched-for source URL."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import unquote, urlparse

from core.fetching.ats.common import (
    AtsResolver,
    AtsResult,
    Status,
    clean_html,
    join_posting_sections,
    parse_posted_date,
)
from core.fetching.hosts import PHENOM_SEARCH


def phenom_locale(prefix: str) -> dict[str, str]:
    """The site's language as its widgets endpoint takes it: /ca/fr is
    fr_ca. Without one the site's default answers (jobs.rbc.com: en_ca 747
    postings, fr_ca 156, 2026-11-01)."""
    country, _, lang = prefix.strip("/").partition("/")
    return {"lang": f"{lang}_{country}", "country": country} if lang else {}


class Phenom(AtsResolver):
    """A Phenom posting, read from its site's jobDetail widget.

    The posting page is a shell: its text is JSON inside a script. On
    2026-11-01 the static tier read 445 to 922 characters of navigation from
    Franklin Templeton, Marsh, Newmont, Battelle, Truist and RBC postings,
    and 8,869 from Merck's, enough to clear the gate with no line of the
    posting. jobDetail answers the whole description (8,996 characters for
    Franklin Templeton 869864).

    It answers a made-up id with HTTP 200 and no job, and no closed posting
    was on record to tell apart from that, so it never says GONE; closure
    comes from the board's authoritative pull. A site runs on its employer's
    host, so like Eightfold this answers only beside the listings URL of a
    Phenom source on the posting's host, and its markers stay empty.
    """

    name = "phenom"
    markers = ()
    _JOB = re.compile(r"^((?:/[a-z]+/[a-z]{2})?)/job/([^/]+)")

    def matches(self, url: str) -> bool:
        return False

    def fetch(self, url: str) -> AtsResult:
        return AtsResult(Status.UNSUPPORTED)

    def listing(self, url: str) -> str | None:
        """The listings URL a Phenom source for this posting would have."""
        parsed = urlparse(url)
        match = self._JOB.match(parsed.path)
        if parsed.scheme != "https" or not match:
            return None
        return f"https://{parsed.netloc.lower()}{match.group(1)}/search-results"

    def fetch_listed(self, url: str, listings_url: str) -> AtsResult | None:
        """The posting's text, or None when listings_url is not the Phenom
        search page of the posting's own site."""
        posting, listing = urlparse(url), urlparse(listings_url)
        job, search = self._JOB.match(posting.path), PHENOM_SEARCH.match(listing.path)
        if (
            not job
            or not search
            or posting.netloc.lower() != listing.netloc.lower()
            or job.group(1) != search.group(1)
        ):
            return None
        resp = self.post(
            f"https://{listing.netloc}/widgets",
            json={
                "ddoKey": "jobDetail",
                "jobId": unquote(job.group(2)),
                **phenom_locale(job.group(1)),
            },
        )
        # The endpoint's path names no posting, so a 404 is the site, not a closure.
        if resp is None or resp.status_code != 200:
            return AtsResult(Status.ERROR, source=self.name)
        data = (resp.json().get("jobDetail") or {}).get("data") or {}
        detail = data.get("job") or {}
        text = detail.get("description")
        result = self.result(
            join_posting_sections(
                detail.get("title"),
                # Each place here is a record, where the search's rows hold strings.
                "; ".join(
                    dict.fromkeys(
                        str(m.get("cityStateCountry") or m.get("locationName"))
                        for m in detail.get("multi_location") or []
                    )
                )
                or detail.get("location"),
                clean_html(text),
            )
            if text
            else None
        )
        return replace(result, posted=parse_posted_date(detail.get("postedDate")))


PHENOM = Phenom()
