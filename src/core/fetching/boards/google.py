"""Fetch listings from Google's careers feed."""

from __future__ import annotations

from typing import Any

from core.fetching.ats import clean_html, join
from core.fetching.boards.common import (
    _get,
    _xml,
    iso_posted_timestamp,
    location_label,
    posting_from_listing,
)
from core.fetching.posting import JobPosting


def _google(url: str, company: str) -> list[JobPosting]:
    """GET https://www.google.com/about/careers/applications/jobs/feed.xml

    One document holds every open posting with its whole text: 3,758 on
    2026-10-16, about 22 MB, rebuilt every few minutes. It states no count,
    and it needs none, because it is not paged. The same day it held every
    posting the sitemap listed, and every posting a full walk of the site's
    search returned. The only urls those had beyond it were five evergreen
    "Open career opportunities" pages (Waymo, Wing, CapitalG), which are not
    postings. The search counts English postings only: its 3,633 was the
    feed's 3,628 en-US postings plus those five pages.

    The search itself is no listing source. robots.txt disallows it, and a
    walk by page returned 3,498 distinct postings of 3,633 rows, because
    consecutive pages come from replicas that disagree (the counts each page
    stated ranged from 3,620 to 3,633).

    A truncated download cannot be read as a short board: the document then
    never closes, and parsing it fails the pull.
    """
    resp = _get(url, headers={"Accept": "text/xml"})
    resp.raise_for_status()
    root = _xml(resp.content)
    if root.tag != "jobs":
        raise ValueError(f"not Google's jobs feed: {resp.content[:200]!r}")
    out: list[JobPosting] = []
    for job in root.iterfind("job"):
        places = [
            location_label(
                {
                    "city": e.findtext("city"),
                    "region": e.findtext("state"),
                    "country": e.findtext("country"),
                }
            )
            for e in job.iterfind("locations/location")
        ]
        title = job.findtext("title")
        raw: dict[str, Any] = {
            e.tag: [c.text for c in e] if e.tag == "categories" else e.text
            for e in job
            if e.tag not in ("description", "locations")
        }
        raw["locations"] = [{c.tag: c.text for c in e} for e in job.iterfind("locations/location")]
        p = posting_from_listing(
            company,
            title,
            places,
            job.findtext("url"),
            iso_posted_timestamp(job.findtext("published")),
            raw=raw,
            description=join(
                title, "; ".join(places), clean_html(job.findtext("description") or "")
            ),
        )
        if p:
            out.append(p)
    return out
