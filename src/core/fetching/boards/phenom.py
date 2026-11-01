"""Fetch listings from the Phenom board format."""

from __future__ import annotations

from urllib.parse import quote, urlparse

from core.fetching.ats.phenom import phenom_locale
from core.fetching.boards.common import (
    PartialPull,
    _post,
    iso_posted_timestamp,
    posting_from_listing,
)
from core.fetching.hosts import PHENOM_SEARCH
from core.fetching.posting import JobPosting

# Asked for more, the search answers 500 rows (Truist, 2026-11-01).
_PHENOM_PAGE = 500


def _phenom(url: str, company: str) -> list[JobPosting]:
    """POST https://{careers site}/widgets {"ddoKey": "refineSearch", ...}

    Phenom career sites run on employers' own hosts, and the listings URL is
    the site's search page, which names the host and the language. The page
    calls the same endpoint; it needs no session, token or cookie. Rows name
    no employer and carry a teaser rather than the text; the Phenom resolver
    reads the text from the site's jobDetail.

    Pages are read by `from`, and an order that shifts between pages loses
    postings. The site answers from replicas that order the same query
    differently and ignore sortBy: Truist's 1,478 postings read 1,144 to
    1,351 distinct when every page opened a new connection, and 1,478 when
    the pages shared one, as the client session's do (Truist, PNC 2,092 and
    Thermo Fisher 3,155, three pulls each, 2026-11-01). So the count is the
    check, and a pull short of it raises PartialPull.
    """
    parsed = urlparse(url)
    search = PHENOM_SEARCH.match(parsed.path)
    if not search:
        raise ValueError(f"{url} is not a Phenom search page")
    origin = f"https://{parsed.netloc}"
    posting = f"{origin}{search.group(1)}/job/"
    locale = phenom_locale(search.group(1))
    seen: dict[str, JobPosting] = {}
    offset = total = 0
    while True:
        resp = _post(
            f"{origin}/widgets",
            json={
                "ddoKey": "refineSearch",
                "from": offset,
                "size": _PHENOM_PAGE,
                "jobs": True,
                "counts": False,
                **locale,
            },
        )
        resp.raise_for_status()
        search_result = resp.json()["refineSearch"]
        data = search_result.get("data") or {}
        # A request it cannot read answers HTTP 200, status 200, totalHits 0
        # and data without `jobs` ("from": "x", 2026-11-01). An empty search
        # carries "jobs": []. Read as a page, the first is an empty board.
        if search_result.get("status") != 200 or "jobs" not in data:
            raise ValueError(f"refineSearch answered no job list: {str(search_result)[:200]}")
        jobs = data["jobs"] or []
        if offset == 0:
            total = int(search_result["totalHits"])
        for j in jobs:
            p = posting_from_listing(
                company,
                j.get("title"),
                j.get("multi_location") or [j.get("cityStateCountry") or j.get("location")],
                posting + quote(str(j["jobId"]), safe="") if j.get("jobId") else None,
                iso_posted_timestamp(j.get("postedDate")),
                raw=j,
            )
            if p:
                seen[p.url] = p
        offset += len(jobs)
        if not jobs or offset >= total:
            break
    postings = list(seen.values())
    if len(postings) < total:
        raise PartialPull(postings)
    return postings
