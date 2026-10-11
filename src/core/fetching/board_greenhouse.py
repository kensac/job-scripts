"""Fetch listings from the Greenhouse board format."""

from __future__ import annotations

from core.fetching.ats import (
    greenhouse_text,
)
from core.fetching.board_common import (
    _get,
    iso_posted_timestamp,
    posting_from_listing,
    with_query_parameters,
)
from core.fetching.posting import JobPosting


def _greenhouse(url: str, company: str) -> list[JobPosting]:
    # content=true returns every posting's text in the one call, so nothing
    # downstream has to fetch the posting to read it.
    resp = _get(with_query_parameters(url, content="true"))
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        location = (j.get("location") or {}).get("name") or ""
        p = posting_from_listing(
            j.get("company_name") or company,
            j.get("title"),
            location.split(";"),
            j.get("absolute_url"),
            iso_posted_timestamp(j.get("first_published")),
            raw=j,
            description=greenhouse_text(j) if j.get("content") else "",
        )
        if p:
            out.append(p)
    return out
