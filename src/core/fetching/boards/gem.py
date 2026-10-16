"""Fetch listings from the Gem board format."""

from __future__ import annotations

from core.fetching.ats import greenhouse_text
from core.fetching.boards.common import _get, iso_posted_timestamp, posting_from_listing
from core.fetching.posting import JobPosting


def _gem(url: str, company: str) -> list[JobPosting]:
    """GET https://api.gem.com/job_board/v0/{board}/job_posts/

    One reply holds every published posting with its text; there is no
    paging and no count. Its urls matched the board page's links one for one
    on Function Health (29) and Bilt (19) on 2026-10-16. A row is Greenhouse's
    job shape without the company.
    """
    resp = _get(url)
    resp.raise_for_status()
    out = []
    for j in resp.json():
        p = posting_from_listing(
            company,
            j.get("title"),
            [(j.get("location") or {}).get("name")],
            j.get("absolute_url"),
            iso_posted_timestamp(j.get("first_published_at")),
            raw=j,
            description=greenhouse_text(j) if j.get("content") else "",
        )
        if p:
            out.append(p)
    return out
