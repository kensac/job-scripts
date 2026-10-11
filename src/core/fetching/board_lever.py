"""Fetch listings from the Lever board format."""

from __future__ import annotations

from core.fetching.ats import (
    lever_text,
)
from core.fetching.board_common import (
    _get,
    posting_from_listing,
)
from core.fetching.posting import JobPosting


def _lever(url: str, company: str) -> list[JobPosting]:
    resp = _get(url)
    resp.raise_for_status()
    out = []
    for j in resp.json():
        cats = j.get("categories") or {}
        p = posting_from_listing(
            company,
            j.get("text"),
            cats.get("allLocations") or [cats.get("location")],
            j.get("hostedUrl"),
            int(j.get("createdAt") or 0) // 1000,
            raw=j,
            description=lever_text(j) if j.get("description") or j.get("lists") else "",
        )
        if p:
            out.append(p)
    return out
