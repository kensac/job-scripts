"""Fetch listings from the Smartrecruiters board format."""

from __future__ import annotations

from core.fetching.board_common import (
    _get,
    iso_posted_timestamp,
    location_label,
    posting_from_listing,
    with_query_parameters,
)
from core.fetching.posting import JobPosting

# SmartRecruiters' public postings API pages 100 at a time whatever limit is
# asked for; measured on BoschGroup, 4,813 postings, 2026-09-05.
_SMARTRECRUITERS_PAGE = 100


def _smartrecruiters(url: str, company: str) -> list[JobPosting]:
    """GET https://api.smartrecruiters.com/v1/companies/{id}/postings

    Names the company on every row. The posting's text is one more call per
    row, which the resolver in core/ats.py makes when the text is needed.
    """
    out: list[JobPosting] = []
    offset = 0
    while True:
        resp = _get(
            with_query_parameters(url, limit=str(_SMARTRECRUITERS_PAGE), offset=str(offset)),
        )
        resp.raise_for_status()
        data = resp.json()
        page = data.get("content") or []
        for j in page:
            org = j.get("company") or {}
            loc = j.get("location") or {}
            p = posting_from_listing(
                org.get("name") or company,
                j.get("name"),
                [loc.get("fullLocation") or location_label(loc)],
                f"https://jobs.smartrecruiters.com/{org['identifier']}/{j['id']}"
                if org.get("identifier") and j.get("id")
                else None,
                iso_posted_timestamp(j.get("releasedDate")),
                raw=j,
            )
            if p:
                out.append(p)
        offset += len(page)
        if not page or offset >= int(data.get("totalFound") or 0):
            return out
