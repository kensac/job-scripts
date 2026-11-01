"""Fetch listings from the PageUp board format."""

from __future__ import annotations

import re

from core.fetching.ats import clean_html, join
from core.fetching.boards.common import (
    _get,
    _xml,
    iso_posted_timestamp,
    posting_from_listing,
)
from core.fetching.posting import JobPosting

# The external careers site's feed. /ci/ is the internal site's.
_PAGEUP_FEED = re.compile(r"/\d+/cw/[a-z-]+/rss/?")
_JOB = "{http://pageuppeople.com/}"
_ATOM = "{http://www.w3.org/2005/Atom}"


def _pageup(url: str, company: str) -> list[JobPosting]:
    """GET https://careers.pageuppeople.com/{instance}/cw/{language}/rss

    One RSS document of every posting on the external careers site, each
    with its whole text (`job:description`; the plain `description` is a
    snippet), unpaged and with no count. Mutual of Omaha's held 68 on
    2026-11-01, the 20 the listing page showed plus the 48 it said were more.
    """
    resp = _get(url, headers={"Accept": "application/rss+xml"})
    resp.raise_for_status()
    root = _xml(resp.content)
    if root.tag != "rss":
        raise ValueError(f"not an RSS feed: {resp.content[:200]!r}")
    out: list[JobPosting] = []
    for item in root.iterfind("./channel/item"):
        title = item.findtext("title")
        place = item.findtext(f"{_JOB}location")
        raw = {
            e.tag.rsplit("}", 1)[-1]: e.text
            for e in item
            if e.tag not in ("description", f"{_JOB}description")
        }
        p = posting_from_listing(
            company,
            title,
            [place],
            item.findtext("link"),
            iso_posted_timestamp(item.findtext(f"{_ATOM}updated")),
            raw=raw,
            description=join(title, place, clean_html(item.findtext(f"{_JOB}description") or "")),
        )
        if p:
            out.append(p)
    return out
