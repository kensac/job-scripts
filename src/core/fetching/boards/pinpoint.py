"""Fetch listings from the Pinpoint board format."""

from __future__ import annotations

from urllib.parse import urlparse

from core.fetching.ats import clean_html, join
from core.fetching.boards.common import _get, posting_from_listing
from core.fetching.posting import JobPosting

# The posting's sections, each under the heading the board gives it.
_PINPOINT_SECTIONS = ("key_responsibilities", "skills_knowledge_expertise", "benefits")


def _pinpoint(url: str, company: str) -> list[JobPosting]:
    """GET https://{tenant}.pinpointhq.com/postings.json

    One JSON reply of every open posting with its whole text, unpaged, with
    no count and no date. It held the same 216 postings as Impulse Space's
    careers page on 2026-11-01. A row's `url` can be on the employer's own
    domain (careers.wolve.com for Wolverine Trading), while the aggregators
    link the tenant host, which serves the same `path`; the tenant host is
    the one kept. A tenant that does not exist answers 404.
    """
    resp = _get(url)
    resp.raise_for_status()
    origin = f"https://{urlparse(url).netloc}"
    out: list[JobPosting] = []
    for j in resp.json()["data"]:
        place = (j.get("location") or {}).get("name")
        sections = [
            join(clean_html(j.get(f"{key}_header") or ""), clean_html(j.get(key) or ""))
            for key in _PINPOINT_SECTIONS
            if j.get(key)
        ]
        p = posting_from_listing(
            company,
            j.get("title"),
            [place],
            f"{origin}{j['path']}" if j.get("path") else None,
            0,
            raw={k: v for k, v in j.items() if k not in _PINPOINT_SECTIONS},
            description=join(
                j.get("title"), place, clean_html(j.get("description") or ""), *sections
            ),
        )
        if p:
            out.append(p)
    return out
