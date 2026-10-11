"""Fetch listings from the Jibe board format."""

from __future__ import annotations

from urllib.parse import urlparse

from core.fetching.ats import (
    clean_html,
    join,
)
from core.fetching.boards.common import (
    PartialPull,
    _get,
    iso_posted_timestamp,
    posting_from_listing,
    with_query_parameters,
)
from core.fetching.posting import JobPosting

# Jibe answers 422 to limit=101 and above (careers.spiritaero.com, 2026-10-05).
_JIBE_PAGE = 100


def _jibe(url: str, company: str) -> list[JobPosting]:
    """GET https://{careers site}/api/jobs?limit=100&page={n}

    iCIMS's hosted career sites (Jibe). page= is one-based (page=0 is a 422),
    every page states totalCount, and a page past the end is empty. Each job
    carries its full text and names its employer. The posting a person opens
    is /jobs/{slug} on the same host, which redirects where a tenant mounts
    its site under a path (AMD's /careers-home).
    """
    host = urlparse(url).netloc
    seen: dict[str, JobPosting] = {}
    rows = total = 0
    page = 1
    while True:
        resp = _get(with_query_parameters(url, limit=str(_JIBE_PAGE), page=str(page)))
        resp.raise_for_status()
        data = resp.json()
        jobs = [j.get("data") or {} for j in data.get("jobs") or []]
        if page == 1:
            total = int(data.get("totalCount") or 0)
        for j in jobs:
            p = posting_from_listing(
                j.get("hiring_organization") or company,
                j.get("title"),
                list(dict.fromkeys(s.strip() for s in (j.get("full_location") or "").split(";"))),
                f"https://{host}/jobs/{j['slug']}" if j.get("slug") else None,
                iso_posted_timestamp(j.get("posted_date")),
                raw={k: v for k, v in j.items() if k not in _JIBE_TEXT},
                description=_jibe_text(j),
            )
            if p:
                seen[p.url] = p
        rows += len(jobs)
        if not jobs or rows >= total:
            break
        page += 1
    postings = list(seen.values())
    if len(postings) != max(rows, total):
        raise PartialPull(postings)
    return postings


_JIBE_TEXT = ("responsibilities", "qualifications")


def _jibe_text(job: dict) -> str:
    """The description, which on most tenants already holds the
    responsibilities and qualifications; V2X left them out of 17 of 765."""
    body = job.get("description") or ""
    extra = [job.get(k) or "" for k in _JIBE_TEXT]
    return clean_html(join(body, *[e for e in extra if e.strip() and e not in body]))
