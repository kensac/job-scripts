"""Fetch listings from the Workday board format."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

from core.fetching.board_common import (
    PartialPull,
    _post,
    posting_from_listing,
)
from core.fetching.board_dates import posted_ts
from core.fetching.posting import JobPosting

# Workday's job-search endpoint returns at most 20 postings per request
# whatever limit is asked for.
_WORKDAY_PAGE = 20

# Some tenants stop a search at 2,000 results: the first page says total=2000
# and every page past offset 2,000 wraps back to the first. Measured on
# 2026-10-05 on 19 of 355 tenants (Airbus, NVIDIA, Walmart); others page
# straight past it (CVS, 18,841).
_WORKDAY_WINDOW = 2000


# A count is not a place. Measured 2026-09-12: 1,002 postings in the catalog
# carry "2 Locations", "3 Locations" and so on as their only location, and 162
# carry none at all. Both defeat a location filter, in opposite directions. A
# count never matches the `locations` vocabulary, so those postings are
# silently EXCLUDED wherever included_locations is set; an empty list is
# deliberately kept by that same predicate, which is how an Accenture posting
# in Jakarta reached a United States board.
_WORKDAY_LOCATION_COUNT = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def _workday_locations(posting: dict) -> list[str]:
    """Where a Workday posting is, from whichever field actually says.

    `locationsText` is the intended field and is right when it names a place.
    Three tenants measured on 2026-09-12 do not send it at all (Accenture,
    Thomson Reuters) or send a count instead (BlackRock, "2 Locations"). The
    place is then only in `externalPath`, which is the posting's own url:
    /job/<Place>/<Title>_<req>. Some postings carry no place segment, and
    those keep returning nothing rather than inventing one.
    """
    text = (posting.get("locationsText") or "").strip()
    if text and not _WORKDAY_LOCATION_COUNT.match(text):
        return [text]
    parts = [segment for segment in (posting.get("externalPath") or "").split("/") if segment]
    if len(parts) >= 3 and parts[0] == "job":
        # Workday slugs a place with hyphens and runs them together for its
        # own separators: "Pernambuco---Recife", "Nova-Lima-Shopping-Alta-Vila".
        # One pass, because replacing "---" first and "-" second eats the
        # hyphen the first replacement just wrote.
        place = re.sub(r"-+", lambda m: " - " if len(m.group()) >= 3 else " ", parts[1]).strip()
        if place:
            return [place]
    return []


def _workday(url: str, company: str) -> list[JobPosting]:
    """POST https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

    A searchText query parameter on the listings URL becomes the search the
    tenant's own careers page would run, which is the only server-side filter
    these boards offer; Boeing returned 334 postings for "new grad" on 2026-09-04.

    A tenant that stops at _WORKDAY_WINDOW is read again in slices, one value
    of its own facets at a time, and the pull is reported partial.
    """
    parsed = urlparse(url)
    search = (parse_qs(parsed.query).get("searchText") or [""])[0]
    endpoint = urlunparse(parsed._replace(query="", fragment=""))
    site = parsed.path.split("/wday/cxs/", 1)[1].split("/")[1]
    base = f"https://{parsed.netloc}/{site}"
    postings, total, facets = _workday_slice(endpoint, base, company, search, {})
    if total != _WORKDAY_WINDOW:
        return postings
    seen = {p.url: p for p in postings}
    for parameter, values in _workday_slicing_facets(facets):
        for value in values:
            part, _, _ = _workday_slice(endpoint, base, company, search, {parameter: [value]})
            seen.update((p.url, p) for p in part)
    raise PartialPull(list(seen.values()))


def _workday_slice(
    endpoint: str, base: str, company: str, search: str, facets: dict[str, list[str]]
) -> tuple[list[JobPosting], int, list[dict]]:
    """Every posting one search returns, its stated total and its facets."""
    out: list[JobPosting] = []
    offset = 0
    # Only the first page carries the count; later pages say total=0. Read
    # per page, that stopped every tenant after two pages: on 2026-10-05, 130
    # of 454 Workday sources held exactly 40 postings (Boeing listed 752), and
    # because the pull is authoritative the rest were retired as closed.
    total: int | None = None
    available: list[dict] = []
    while True:
        resp = _post(
            endpoint,
            json={
                "appliedFacets": facets,
                "limit": _WORKDAY_PAGE,
                "offset": offset,
                "searchText": search,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        page = data.get("jobPostings") or []
        for j in page:
            p = posting_from_listing(
                company,
                j.get("title"),
                _workday_locations(j),
                base + j.get("externalPath", "") if j.get("externalPath") else None,
                posted_ts(j.get("postedOn") or ""),
            )
            if p:
                out.append(p)
        if total is None:
            total = int(data.get("total") or 0)
            available = data.get("facets") or []
        offset += len(page)
        # Past the window a capped tenant wraps to its first page, so the
        # window bounds the loop even where the total does not.
        if not page or offset >= min(total, _WORKDAY_WINDOW):
            return out, total, available


def _workday_slicing_facets(facets: list[dict]) -> list[tuple[str, list[str]]]:
    """The two facets that cover the most postings with every value under the window.

    Two, because a posting carrying no value of the first is invisible to its
    slices; on Airbus the best single facet covered 2,755 of about 2,940.
    Facets whose largest value reaches the window cannot be read whole.
    """

    def expand(parameter: str, values: list[dict]):
        # A value carrying its own facetParameter and values is a facet in its
        # own right (Airbus's locationMainGroup holds locationCountry), and its
        # ids are only accepted under that inner name: under the outer one the
        # endpoint answers 400.
        leaves = [v for v in values if "values" not in v]
        if leaves:
            yield parameter, leaves
        for group in values:
            if "values" in group and group.get("facetParameter"):
                yield from expand(group["facetParameter"], group["values"])

    usable = []
    for facet in facets:
        for parameter, values in expand(facet["facetParameter"], facet.get("values") or []):
            counts = [int(v.get("count") or 0) for v in values]
            if max(counts) < _WORKDAY_WINDOW:
                usable.append((sum(counts), parameter, [v["id"] for v in values]))
    usable.sort(key=lambda u: u[0], reverse=True)
    return [(parameter, ids) for _, parameter, ids in usable[:2]]
