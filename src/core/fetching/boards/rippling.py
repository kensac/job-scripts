"""Fetch listings from the Rippling board format."""

from __future__ import annotations

import re

from core.fetching.boards.common import (
    PartialPull,
    _get,
    posting_from_listing,
    with_query_parameters,
)
from core.fetching.posting import JobPosting

# The listings URL is the board's jobs endpoint, never its public page.
_RIPPLING_BOARD = re.compile(r"/api/v2/board/[^/]+/jobs/?")
# The largest pageSize the board answers; 1,001 is a 400 (2026-10-16).
_RIPPLING_PAGE = 1000


def _rippling(url: str, company: str) -> list[JobPosting]:
    """GET https://ats.rippling.com/api/v2/board/{board}/jobs?page=&pageSize=

    A row is one posting at one place: a posting open in three cities is
    three rows with one id, and `totalItems` counts rows (Loop: 44 rows, 24
    postings, 2026-10-16). Pages are zero-based, every page states the count,
    and a page past the end is empty. Rows carry no text, company or date;
    the resolver reads them from the posting.
    """
    by_id: dict[str, dict] = {}
    places: dict[str, list[str]] = {}
    rows = page = 0
    total = None
    while total is None or rows < total:
        resp = _get(with_query_parameters(url, page=str(page), pageSize=str(_RIPPLING_PAGE)))
        resp.raise_for_status()
        data = resp.json()
        total = data["totalItems"] if total is None else total
        items = data.get("items") or []
        if not items:
            break
        for j in items:
            by_id.setdefault(j["id"], j)
            places.setdefault(j["id"], []).extend(
                loc.get("name") for loc in j.get("locations") or []
            )
        rows += len(items)
        page += 1
    out = []
    for job_id, j in by_id.items():
        p = posting_from_listing(
            company, j.get("name"), list(dict.fromkeys(places[job_id])), j.get("url"), 0, raw=j
        )
        if p:
            out.append(p)
    if rows < total:
        raise PartialPull(out)
    return out
