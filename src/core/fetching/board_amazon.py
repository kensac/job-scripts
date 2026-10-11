"""Fetch listings from the Amazon board format."""

from __future__ import annotations

import json

from core.fetching.ats import (
    clean_html,
    join,
)
from core.fetching.board_common import (
    PartialPull,
    _get,
    posting_from_listing,
)
from core.fetching.board_dates import posted_ts
from core.fetching.posting import JobPosting

# amazon.jobs answers "Result limit cannot be greater than 100" past 100 a
# request, and refuses a page reaching past row 10,000 of a search ("Cannot
# return more than 10000 results at once", with HTTP 200, hits 0 and no jobs).
# Its `hits` stops at the window too: the unfiltered search said 10,000 for a
# board of 22,295 on 2026-10-05.
_AMAZON_PAGE = 100
_AMAZON_WINDOW = 10_000

# The row's text, kept out of raw for the reason _TEXT_FIELDS gives.
_AMAZON_TEXT = frozenset(
    {"description", "description_short", "basic_qualifications", "preferred_qualifications"}
)


def _amazon(url: str, company: str) -> list[JobPosting]:
    """GET https://www.amazon.jobs/en/search.json

    The board is past the window, so it is read one job category at a time.
    Categories partition it: on 2026-10-05 their counts summed to 22,295, the
    same as the country facet's, and the largest held 3,279. A pull is complete
    only when the categories account for every posting the country facet
    counts, no category reaches the window, and each category's pages yielded
    as many distinct postings as its first page stated; otherwise it is partial.
    """
    first = _amazon_get(
        url, {"result_limit": 1, "facets[]": ["category", "normalized_country_code"]}
    )
    categories = _amazon_facet(first, "category_facet")
    complete = sum(categories.values()) >= sum(
        _amazon_facet(first, "normalized_country_code_facet").values()
    )
    seen: dict[str, JobPosting] = {}
    for category in categories:
        part, whole = _amazon_slice(url, company, category)
        complete = complete and whole
        seen.update((p.url, p) for p in part)
    if not complete:
        raise PartialPull(list(seen.values()))
    return list(seen.values())


def _amazon_get(url: str, params: dict) -> dict:
    resp = _get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"amazon.jobs refused {params}: {data['error']}")
    return data


def _amazon_facet(data: dict, name: str) -> dict[str, int]:
    """A facet as {value: count}; amazon.jobs sends it as one-key objects."""
    return {
        k: int(v) for entry in (data.get("facets") or {}).get(name) or [] for k, v in entry.items()
    }


def _amazon_slice(url: str, company: str, category: str) -> tuple[list[JobPosting], bool]:
    """One category's postings, and whether they are every one it stated."""
    seen: dict[str, JobPosting] = {}
    offset = 0
    total: int | None = None
    while True:
        data = _amazon_get(
            url,
            {
                "result_limit": min(_AMAZON_PAGE, _AMAZON_WINDOW - offset),
                "offset": offset,
                "category[]": category,
                "sort": "recent",
            },
        )
        page = data.get("jobs") or []
        for j in page:
            p = posting_from_listing(
                company,
                j.get("title"),
                [json.loads(x).get("normalizedLocation") for x in j.get("locations") or []]
                or [j.get("normalized_location")],
                f"https://www.amazon.jobs{j['job_path']}" if j.get("job_path") else None,
                posted_ts(j.get("posted_date") or ""),
                raw={k: v for k, v in j.items() if k not in _AMAZON_TEXT},
                description=_amazon_text(j),
            )
            if p:
                seen[p.url] = p
        if total is None:
            total = int(data.get("hits") or 0)
        offset += len(page)
        if not page or offset >= min(total, _AMAZON_WINDOW):
            # A posting that closes mid-read moves every later row up one, so
            # a page boundary skips an open posting; that shows as fewer
            # distinct postings than the first page's count. One that opens
            # moves them down and repeats a row, which loses nothing.
            return list(seen.values()), total < _AMAZON_WINDOW and len(seen) >= total


def _amazon_text(j: dict) -> str:
    sections = (
        ("Basic qualifications", clean_html(j.get("basic_qualifications") or "")),
        ("Preferred qualifications", clean_html(j.get("preferred_qualifications") or "")),
    )
    return join(
        j.get("title"),
        j.get("normalized_location"),
        clean_html(j.get("description") or ""),
        *[join(heading, text) for heading, text in sections if text],
    )
