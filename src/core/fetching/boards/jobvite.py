"""Fetch listings from the Jobvite board format."""

from __future__ import annotations

import datetime
import re
from urllib.parse import urlparse

from core.fetching.ats import clean_html, join
from core.fetching.boards.common import _get, _xml, posting_from_listing
from core.fetching.posting import JobPosting

# The listings URL is the careers page, jobs.jobvite.com/{slug}/jobs.
_JOBVITE_BOARD = re.compile(r"/([^/]+)/jobs/?")
# The fields kept as the raw record. A row also names its hiring team, its
# referral bonus plan and its agency access, which are not the posting's.
_JOBVITE_FIELDS = (
    "id",
    "requisitionid",
    "title",
    "category",
    "jobtype",
    "region",
    "location",
    "date",
    "parentId",
)
# The careers page names the company id the feed is keyed by.
_JOBVITE_COMPANY_ID = re.compile(r"getCompanyId\(\)\s*\{\s*return\s*'([A-Za-z0-9]+)'")


def _jobvite_posted(text: str | None) -> int:
    try:
        day = datetime.datetime.strptime(f"{text} +0000", "%m/%d/%Y %z")
    except ValueError:
        return 0
    return int(day.timestamp())


def _jobvite(url: str, company: str) -> list[JobPosting]:
    """GET https://jobs.jobvite.com/{slug}/jobs, then
    GET https://app.jobvite.com/CompanyJobs/Xml.aspx?c={company id}

    The feed is keyed by a company id, which only the careers page names, and
    its rows link app.jobvite.com, which redirects to
    jobs.jobvite.com/careers/{slug}/job/{id}; so the page is read for the id
    and the slug comes from the listings URL. The feed holds every open
    posting with its whole text, unpaged and with no count. A posting open in
    several places is one row per place: the first with the posting's id, the
    rest `{id}-{suffix}` with a `parentId`, all linking the one posting. On
    Nutanix on 2026-11-01 that was 392 rows and 270 ids, and the site's
    search listed the same 270 ids.
    """
    slug = _JOBVITE_BOARD.fullmatch(urlparse(url).path)
    if not slug:
        raise ValueError(f"not a Jobvite careers page: {url}")
    page = _get(url, headers={"Accept": "text/html"})
    page.raise_for_status()
    company_id = _JOBVITE_COMPANY_ID.search(page.text)
    if not company_id:
        raise ValueError(f"no Jobvite company id on {url}")
    resp = _get(
        f"https://app.jobvite.com/CompanyJobs/Xml.aspx?c={company_id.group(1)}",
        headers={"Accept": "text/xml"},
    )
    resp.raise_for_status()
    root = _xml(resp.content)
    if root.tag != "result":
        raise ValueError(f"not a Jobvite feed: {resp.content[:200]!r}")
    rows: dict[str, dict] = {}
    places: dict[str, list[str]] = {}
    texts: dict[str, str] = {}
    for job in root.iterfind("job"):
        job_id = (job.findtext("id") or "").split("-", 1)[0]
        if not job_id:
            continue
        raw = {key: job.findtext(key) for key in _JOBVITE_FIELDS}
        rows.setdefault(job_id, raw)
        texts.setdefault(job_id, clean_html(job.findtext("description") or ""))
        places.setdefault(job_id, []).append(raw.get("location") or "")
    out: list[JobPosting] = []
    for job_id, raw in rows.items():
        where = list(dict.fromkeys(x for x in places[job_id] if x))
        p = posting_from_listing(
            company,
            raw.get("title"),
            where,
            f"https://jobs.jobvite.com/{slug.group(1)}/job/{job_id}",
            _jobvite_posted(raw.get("date")),
            raw=raw,
            description=join(raw.get("title"), "; ".join(where), texts[job_id]),
        )
        if p:
            out.append(p)
    return out
