"""Fetch listings from the Ashby board format."""

from __future__ import annotations

from core.fetching.ats import (
    ashby_text,
)
from core.fetching.boards.common import (
    _get,
    iso_posted_timestamp,
    posting_from_listing,
    with_query_parameters,
)
from core.fetching.posting import JobPosting


def _ashby(url: str, company: str) -> list[JobPosting]:
    # The board call already carries every posting's description; asking for
    # compensation too makes it the same text the resolver assembles.
    resp = _get(with_query_parameters(url, includeCompensation="true"))
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        if not j.get("isListed", True):
            continue
        p = posting_from_listing(
            company,
            j.get("title"),
            [j.get("location"), *[s.get("location") for s in j.get("secondaryLocations") or []]],
            j.get("jobUrl"),
            iso_posted_timestamp(j.get("publishedAt")),
            raw=j,
            description=ashby_text(j) if j.get("descriptionHtml") else "",
        )
        if p:
            out.append(p)
    return out
