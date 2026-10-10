"""The fallback listing fetcher: a board that is not one of the known formats.

core/boards.py routes a listings URL to a per-format fetcher and falls back to
fetch_job_postings here, which reads a plain JSON feed and carries the airtable
and jobright shapes with it.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any
from urllib.parse import unquote, urlparse

import ftfy

from core.fetching.client import pace, session
from core.fetching.posting import JobPosting
from core.fetching.urls import normalize_url

logger = logging.getLogger(__name__)

AIRTABLE_HOST = "https://airtable.com"
AIRTABLE_HEADERS = {
    "x-requested-with": "XMLHttpRequest",
    "x-time-zone": "America/New_York",
    "x-user-locale": "en",
    "Accept": "application/json",
}


def _parse_airtable_date(value: Any) -> int:
    if not value:
        return 0
    try:
        return int(datetime.datetime.fromisoformat(str(value).strip()).timestamp())
    except ValueError:
        return 0


def fetch_airtable_postings(url: str) -> list[JobPosting]:
    pace(url)
    page = session.get(url, headers=AIRTABLE_HEADERS)
    page.raise_for_status()
    match = re.search(r'urlWithParams:\s*"([^"]+)"', page.text)
    if not match:
        raise ValueError("Airtable share payload not found")
    path = match.group(1).encode().decode("unicode_escape")
    app_id = re.search(r'"applicationId":"(app[A-Za-z0-9]+)"', unquote(path))
    if not app_id:
        raise ValueError("Airtable applicationId not found")
    pace(AIRTABLE_HOST)
    resp = session.get(
        AIRTABLE_HOST + path,
        headers={**AIRTABLE_HEADERS, "x-airtable-application-id": app_id.group(1)},
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})

    table = (data or {}).get("table", {})
    name_by_id = {c["id"]: c.get("name", "") for c in table.get("columns", [])}

    postings: list[JobPosting] = []
    for row in table.get("rows", []):
        values = {
            name_by_id.get(cid, ""): val for cid, val in row.get("cellValuesByColumnId", {}).items()
        }

        apply_cell = values.get("Apply")
        raw_url = apply_cell.get("url", "") if isinstance(apply_cell, dict) else ""
        if not raw_url:
            continue

        location = ftfy.fix_text(str(values.get("Location") or ""))
        locations = [loc.strip() for loc in location.splitlines() if loc.strip()]

        postings.append(
            JobPosting(
                company=ftfy.fix_text(str(values.get("Company", ""))),
                locations=locations,
                title=ftfy.fix_text(str(values.get("Position Title", ""))),
                url=normalize_url(raw_url),
                terms=[],
                active=True,
                date_posted=_parse_airtable_date(values.get("Date")),
                raw_url=raw_url,
            )
        )

    logger.info(f"Fetched {len(postings)} job postings from Airtable.")
    return postings


def fetch_jobright_postings(url: str) -> list[JobPosting]:
    """Jobright minisites (jobright.ai/minisites-jobs/...) render their newest
    ~50 jobs server-side in __NEXT_DATA__; hourly cycles accumulate coverage.
    Apply links are jobright interstitials - the employer URL is login-gated."""
    import json as _json

    pace(url)
    resp = session.get(url, headers=AIRTABLE_HEADERS)
    resp.raise_for_status()
    match = re.search(r'__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, re.S)
    if not match:
        raise ValueError("no __NEXT_DATA__ payload on page")
    jobs = _json.loads(match.group(1))["props"]["pageProps"]["initialJobs"]
    postings: list[JobPosting] = []
    for j in jobs:
        raw_apply = j.get("applyUrl") or ""
        apply_url = raw_apply.split("?")[0]
        if not apply_url.startswith("http"):
            continue
        postings.append(
            JobPosting(
                company=ftfy.fix_text(str(j.get("company", ""))),
                locations=[p.strip() for p in str(j.get("location") or "").split(";") if p.strip()],
                title=ftfy.fix_text(str(j.get("title", ""))),
                url=apply_url,
                terms=[],
                active=True,
                date_posted=int(j.get("postedDate") or 0) // 1000,
                raw_url=raw_apply,
            )
        )
    logger.info(f"Fetched {len(postings)} job postings from jobright minisite.")
    return postings


def fetch_job_postings(url: str) -> list[JobPosting]:
    if "airtable.com" in urlparse(url).netloc:
        return fetch_airtable_postings(url)

    if "jobright.ai" in urlparse(url).netloc:
        return fetch_jobright_postings(url)

    pace(url)
    response = session.get(url)
    response.raise_for_status()
    data = response.json()

    postings: list[JobPosting] = []
    for entry in data:
        terms: list[str] = []
        if "terms" in entry:
            terms = entry["terms"] if isinstance(entry["terms"], list) else []
        elif "seasons" in entry:
            terms = entry["seasons"] if isinstance(entry["seasons"], list) else []

        raw_url = entry.get("url", "")
        normalized_url = normalize_url(raw_url)

        job = JobPosting(
            company=entry.get("company_name", ""),
            locations=(
                entry.get("locations", []) if isinstance(entry.get("locations"), list) else []
            ),
            title=entry.get("title", ""),
            url=normalized_url,
            terms=terms,
            active=bool(entry.get("active", False)),
            date_posted=int(entry.get("date_posted", 0)),
            raw_url=raw_url,
        )

        postings.append(job)
    logger.info(f"Fetched {len(postings)} job postings.")
    return postings
