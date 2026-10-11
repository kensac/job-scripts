"""Fetch listings from the Ibm board format."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, urlunparse

from core.fetching.board_common import (
    PartialPull,
    _post,
    posting_from_listing,
)
from core.fetching.posting import JobPosting

# IBM's search API answers 400 to a size above 100 (200 and 1,000 tried,
# 2026-10-05).
_IBM_PAGE = 100

# The careers index names its facets by slot. Measured 2026-10-05 against the
# careers search page's own filters: 05 country, 08 area of work, 17 work
# arrangement, 18 position type, 19 "City, CC" or "Multiple Cities", and
# field_text_01 the job id in the posting url. The API refuses a _source
# naming any field outside its allow list, so these are asked for by name.
_IBM_FIELDS = [
    "title",
    "url",
    "field_text_01",
    "field_keyword_05",
    "field_keyword_08",
    "field_keyword_17",
    "field_keyword_18",
    "field_keyword_19",
]


def _ibm(url: str, company: str) -> list[JobPosting]:
    """POST https://www-api.ibm.com/search/api/v2, the search behind
    www.ibm.com/careers/search.

    Pages by search_after on _id. Paging by from repeats rows: the site's own
    sort ties on every posting of an empty search, and 20 shards break the tie
    differently per request, so a from/size walk of 2,002 postings returned
    1,890 distinct on 2026-10-05. A from walk also stops at from+size 10,000.
    The cursor reads the index to its end, and the first page's count is
    then only the check that nothing was lost on the way.

    No text is carried. `description` is a 256-character snippet, and `body`
    is the posting's prose without its headings, education or years of
    experience (134730: 3,668 characters of a 7,663-character page), which
    stored as the posting would read as complete and be judged short. The
    posting page is fetched for the text like any other.
    """
    body: dict[str, Any] = {
        "appId": "careers",
        "scopes": ["careers2"],
        "query": {"bool": {"must": []}},
        "lang": "zz",
        "localeSelector": {},
        "sm": {"query": "", "lang": "zz"},
        "_source": _IBM_FIELDS,
        "size": _IBM_PAGE,
        "sort": [{"_id": "asc"}],
    }
    endpoint = urlunparse(urlparse(url)._replace(query="", fragment=""))
    out: list[JobPosting] = []
    total: int | None = None
    while True:
        resp = _post(endpoint, json=body)
        resp.raise_for_status()
        hits = resp.json()["hits"]
        page = hits.get("hits") or []
        if total is None:
            total = int((hits.get("total") or {}).get("value") or 0)
        for h in page:
            j = h.get("_source") or {}
            place = j.get("field_keyword_19")
            p = posting_from_listing(
                company,
                j.get("title"),
                # "Multiple Cities" is no more a place than Workday's "2
                # Locations" (349 of 2,013 on 2026-10-05); the country is.
                [j.get("field_keyword_05") if place == "Multiple Cities" else place],
                j.get("url"),
                0,
                raw=j,
            )
            if p:
                out.append(p)
        if len(page) < _IBM_PAGE:
            break
        body["search_after"] = page[-1]["sort"]
    if len(out) < total:
        raise PartialPull(out)
    return out
