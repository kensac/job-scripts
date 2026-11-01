"""Fetch listings from the Breezy HR board format."""

from __future__ import annotations

from core.fetching.boards.common import _get, iso_posted_timestamp, posting_from_listing
from core.fetching.posting import JobPosting


def _breezy(url: str, company: str) -> list[JobPosting]:
    """GET https://{tenant}.breezy.hr/json

    One JSON list of every open posting, unpaged and with no count or text.
    It matched the careers page's links one for one on JND (25), NucleusTeq
    (28) and Reveleer (17) on 2026-11-01. The posting page is server
    rendered, and the static tier reads it whole. A tenant that does not
    exist answers 404.
    """
    resp = _get(url)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        raise ValueError(f"not a Breezy listing: {str(rows)[:200]}")
    out: list[JobPosting] = []
    for j in rows:
        places = [loc.get("name") for loc in j.get("locations") or []]
        p = posting_from_listing(
            company,
            j.get("name"),
            places or [(j.get("location") or {}).get("name")],
            j.get("url"),
            iso_posted_timestamp(j.get("published_date")),
            raw=j,
        )
        if p:
            out.append(p)
    return out
