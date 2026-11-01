"""Fetch listings from the UKG Pro Recruiting (UltiPro) board format."""

from __future__ import annotations

import re

from core.fetching.ats.common import ukg_place
from core.fetching.boards.common import (
    PartialPull,
    _post,
    iso_posted_timestamp,
    posting_from_listing,
)
from core.fetching.posting import JobPosting

_UKG_HOST = re.compile(r"recruiting\d*\.ultipro\.(?:com|ca)")
# The listings URL is the board's search endpoint, never its public page.
_UKG_SEARCH = re.compile(r"/[^/]+/JobBoard/[0-9a-fA-F-]{36}/JobBoardView/LoadSearchResults")
# The board page asks for 50; 100 is two of its pages. Every size up to 1,000
# answered whole on Salas O'Brien's 447 postings (2026-11-01).
_UKG_PAGE = 100


def _ukg(url: str, company: str) -> list[JobPosting]:
    """POST https://recruiting.ultipro.com/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults

    The search the board page itself calls; it takes Top and Skip and states
    `totalCount`. A page past the end is empty and states 0, so the count is
    read off the first page. A board that does not exist is a 404. Rows carry
    no company and only a summary of the text; the resolver reads the text
    from the posting page. Walks at 50 and 100 returned 447 distinct of 447
    (Salas O'Brien, 2026-11-01); a pull short of the count is partial.
    """
    board = url.split("/JobBoardView/", 1)[0]
    seen: dict[str, JobPosting] = {}
    total: int | None = None
    skip = 0
    while True:
        resp = _post(
            url,
            json={
                "opportunitySearch": {
                    "Top": _UKG_PAGE,
                    "Skip": skip,
                    "QueryString": "",
                    "OrderBy": [
                        {
                            "Value": "postedDateDesc",
                            "PropertyName": "PostedDate",
                            "Ascending": False,
                        }
                    ],
                    "Filters": [],
                }
            },
        )
        resp.raise_for_status()
        data = resp.json()
        page = data["opportunities"]
        if total is None:
            total = int(data["totalCount"])
        for j in page:
            p = posting_from_listing(
                company,
                j.get("Title"),
                list(dict.fromkeys(ukg_place(loc) for loc in j.get("Locations") or [])),
                f"{board}/OpportunityDetail?opportunityId={j['Id']}" if j.get("Id") else None,
                iso_posted_timestamp(j.get("PostedDate")),
                raw=j,
            )
            if p:
                seen[p.url] = p
        skip += len(page)
        if not page or skip >= total:
            break
    postings = list(seen.values())
    if len(postings) < total:
        raise PartialPull(postings)
    return postings
