"""Fetch listings from the JazzHR board format."""

from __future__ import annotations

from core.fetching.ats import clean_html, join
from core.fetching.boards.common import _get, _xml, location_label, posting_from_listing
from core.fetching.posting import JobPosting


def _jazzhr(url: str, company: str) -> list[JobPosting]:
    """GET https://app.jazz.co/feeds/export/jobs/{subdomain}

    The export feed JazzHR publishes for job boards: one XML document of every
    open posting with its whole text, no paging and no date. Its urls matched
    the links on {subdomain}.applytojob.com/apply one for one on Space
    Dynamics Laboratory (23) on 2026-11-01. A subdomain that does not exist
    answers 404.
    """
    resp = _get(url, headers={"Accept": "text/xml"})
    resp.raise_for_status()
    root = _xml(resp.content)
    if root.tag != "jobs":
        raise ValueError(f"not a JazzHR feed: {resp.content[:200]!r}")
    out: list[JobPosting] = []
    for job in root.iterfind("job"):
        raw = {e.tag: e.text for e in job if e.tag not in ("description", "buttons")}
        place = location_label(
            {"city": raw.get("city"), "region": raw.get("state"), "country": raw.get("country")}
        )
        p = posting_from_listing(
            company,
            raw.get("title"),
            [place],
            raw.get("url"),
            0,
            raw=raw,
            description=join(
                raw.get("title"), place, clean_html(job.findtext("description") or "")
            ),
        )
        if p:
            out.append(p)
    return out
