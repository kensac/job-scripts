"""Fetch listings from the Paylocity board format."""

from __future__ import annotations

import json
import re

from core.fetching.boards.common import _get, iso_posted_timestamp, posting_from_listing
from core.fetching.posting import JobPosting

# The board page, recruiting.paylocity.com/Recruiting/Jobs/All/{guid}, with or
# without the company slug Paylocity appends; the path is case-insensitive.
PAYLOCITY_BOARD = re.compile(r"/recruiting/jobs/all/[0-9a-f-]{36}(?:/[^/]*)?/?", re.IGNORECASE)
_PAGE_DATA = "window.pageData = "


def _paylocity_place(job: dict) -> str:
    loc = job.get("JobLocation") or {}
    place = ", ".join(p for p in (loc.get("City"), loc.get("State")) if p)
    return place or job.get("LocationName") or ""


def _paylocity(url: str, company: str) -> list[JobPosting]:
    """GET https://recruiting.paylocity.com/Recruiting/Jobs/All/{guid}

    The board page carries every open posting in one `window.pageData`
    object, unpaged and with no count: 345 on Evereve, the largest of 22
    boards read on 2026-11-01. Paylocity's job feed
    (/recruiting/v2/api/feed/jobs/{guid}) answered no jobs on all 22, so it
    is not the listing. A page without the object fails the pull. Rows carry
    the first 110 characters of the text; the posting page serves the whole
    text as JobPosting JSON-LD, so the static tier reads it. Internal
    postings are not public and are skipped.
    """
    resp = _get(url)
    resp.raise_for_status()
    start = resp.text.find(_PAGE_DATA)
    if start < 0:
        raise ValueError(f"no pageData on {url}")
    data, _ = json.JSONDecoder().raw_decode(resp.text, start + len(_PAGE_DATA))
    out = []
    for j in data["Jobs"]:
        if j.get("IsInternal"):
            continue
        p = posting_from_listing(
            company,
            j.get("JobTitle"),
            [_paylocity_place(j), "Remote" if j.get("IsRemote") else None],
            f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{j['JobId']}"
            if j.get("JobId")
            else None,
            iso_posted_timestamp(j.get("PublishedDate")),
            raw=j,
        )
        if p:
            out.append(p)
    return out
