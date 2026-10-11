"""Fetch listings from the Apple board format."""

from __future__ import annotations

from core.fetching.boards.common import (
    PartialPull,
    _post,
    iso_posted_timestamp,
    posting_from_listing,
)
from core.fetching.posting import JobPosting

# jobs.apple.com answers 20 postings a page whatever size is asked (limit,
# pageSize, size, rows, perPage and count all tried 2026-10-05).
_APPLE_PAGE = 20


def _apple(url: str, company: str) -> list[JobPosting]:
    """POST https://jobs.apple.com/api/v1/search

    One row per posting and location, each its own public page at
    /en-us/details/{id}/{slug}, which is how the careers site links them. The
    row carries a summary, not the posting's text, so the text comes from the
    Apple resolver in core/fetching/ats/__init__.py.

    Sorted newest, the managed pipeline roles (evergreen retail openings) are
    stamped with the request's own time and so tie at the top, in an order
    each request draws afresh: one pass of 6,192 on 2026-10-05 returned two of
    them twice and two never, the same in each of three runs. The pages that
    held them are read again until the count is reached or a round finds
    nothing new; short of the count the pull is partial.
    """

    def page(number: int) -> tuple[list[dict], int]:
        resp = _post(
            url,
            # Without "format" every page is empty and says totalRecords 0,
            # with a 200, which reads as an empty board. "filters" missing is
            # a 436. Both measured 2026-10-05; the body is the careers site's.
            json={
                "query": "",
                "filters": {},
                "page": number,
                "locale": "en-us",
                "sort": "newest",
                "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
            },
        )
        resp.raise_for_status()
        res = resp.json().get("res") or {}
        return res.get("searchResults") or [], int(res.get("totalRecords") or 0)

    seen: dict[str, JobPosting] = {}

    def keep(rows: list[dict]) -> None:
        for j in rows:
            places = [
                ", ".join(dict.fromkeys(x for x in (loc.get("name"), loc.get("countryName")) if x))
                for loc in j.get("locations") or []
            ]
            p = posting_from_listing(
                company,
                j.get("postingTitle"),
                places,
                # A pipeline row's id is "PIPE-<n>" and its page is /details/<n>.
                f"https://jobs.apple.com/en-us/details/{j['id'].removeprefix('PIPE-')}"
                f"/{j['transformedPostingTitle']}"
                if j.get("id") and j.get("transformedPostingTitle")
                else None,
                # A managed role's date is the request's, not the posting's.
                0 if j.get("managedPipelineRole") else iso_posted_timestamp(j.get("postDateInGMT")),
                raw=j,
            )
            if p:
                seen[p.url] = p

    rows, total = page(1)
    unstable: list[int] = []
    number = 1
    # Every page restates the count except the empty one past the end, which
    # says 0; the first page's count is the one the pull is held to.
    while rows:
        keep(rows)
        if any(j.get("managedPipelineRole") for j in rows):
            unstable.append(number)
        if number * _APPLE_PAGE >= total:
            break
        number += 1
        rows, _ = page(number)
    while len(seen) < total and unstable:
        before = len(seen)
        for number in unstable:
            keep(page(number)[0])
        if len(seen) == before:
            break
    if len(seen) < total:
        raise PartialPull(list(seen.values()))
    return list(seen.values())
