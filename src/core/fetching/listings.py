"""The fallback listing fetcher: a board that is not one of the known formats.

core/boards.py routes a listings URL to a per-format fetcher and falls back to
fetch_job_postings here, which reads a plain JSON feed and carries the airtable
and jobright shapes with it.
"""

from __future__ import annotations

import datetime
import logging
import random
import re
import time
from typing import Any
from urllib.parse import unquote, urlparse

import ftfy
import requests

from core.fetching.posting import JobPosting
from core.fetching.urls import normalize_url

logger = logging.getLogger(__name__)

AIRTABLE_HOST = "https://airtable.com"
AIRTABLE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
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


class ExponentialBackoff:
    def __init__(
        self,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
    ):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.attempt = 0

    def wait(self) -> None:
        if self.attempt == 0:
            self.attempt += 1
            return

        delay = min(
            self.base_delay * (self.backoff_factor ** (self.attempt - 1)),
            self.max_delay,
        )
        jitter = delay * 0.25 * (2 * random.random() - 1)  # noqa: S311 - backoff spacing, not a secret
        final_delay = max(0, delay + jitter)

        logger.debug(f"Exponential backoff: waiting {final_delay:.2f}s (attempt {self.attempt})")
        time.sleep(final_delay)
        self.attempt += 1

    def reset(self) -> None:
        self.attempt = 0


def fetch_airtable_postings(
    url: str, timeout: float = 30.0, max_retries: int = 3
) -> list[JobPosting]:
    backoff = ExponentialBackoff()
    data: dict | None = None

    for attempt in range(max_retries + 1):
        try:
            page = requests.get(url, headers=AIRTABLE_HEADERS, timeout=timeout)
            page.raise_for_status()

            match = re.search(r'urlWithParams:\s*"([^"]+)"', page.text)
            if not match:
                raise ValueError("Airtable share payload not found")
            path = match.group(1).encode().decode("unicode_escape")

            app_id = re.search(r'"applicationId":"(app[A-Za-z0-9]+)"', unquote(path))
            if not app_id:
                raise ValueError("Airtable applicationId not found")

            resp = requests.get(
                AIRTABLE_HOST + path,
                headers={**AIRTABLE_HEADERS, "x-airtable-application-id": app_id.group(1)},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            backoff.reset()
            break

        except (requests.RequestException, ValueError) as exc:
            if attempt == max_retries:
                logger.exception(
                    f"Failed to fetch Airtable postings after {max_retries + 1} attempts"
                )
                return []
            logger.warning(
                f"Airtable attempt {attempt + 1} failed: {exc}, retrying with backoff..."
            )
            backoff.wait()

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


def fetch_jobright_postings(url: str, max_retries: int = 3) -> list[JobPosting]:
    """Jobright minisites (jobright.ai/minisites-jobs/...) render their newest
    ~50 jobs server-side in __NEXT_DATA__; hourly cycles accumulate coverage.
    Apply links are jobright interstitials - the employer URL is login-gated."""
    import json as _json

    backoff = ExponentialBackoff()
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=AIRTABLE_HEADERS, timeout=20)
            resp.raise_for_status()
            match = re.search(
                r'__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, re.S
            )
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
                        locations=[
                            p.strip() for p in str(j.get("location") or "").split(";") if p.strip()
                        ],
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
        except Exception as exc:
            if attempt >= max_retries:
                logger.exception("Failed to fetch jobright postings")
                return []
            logger.warning(f"Jobright attempt {attempt + 1} failed: {exc}, retrying...")
            backoff.wait()
    return []


def fetch_job_postings(url: str, timeout: float = 10.0, max_retries: int = 3) -> list[JobPosting]:
    if "airtable.com" in urlparse(url).netloc:
        return fetch_airtable_postings(url, max_retries=max_retries)

    if "jobright.ai" in urlparse(url).netloc:
        return fetch_jobright_postings(url, max_retries=max_retries)

    backoff = ExponentialBackoff()

    for attempt in range(max_retries + 1):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_time = int(retry_after)
                    logger.warning(f"Rate limited, waiting {wait_time}s as per Retry-After header")
                    time.sleep(wait_time)
                else:
                    logger.warning(
                        f"Rate limited, using exponential backoff (attempt {attempt + 1})"
                    )
                    backoff.wait()
                continue

            data = response.json()
            backoff.reset()
            break

        except requests.RequestException as exc:
            if attempt == max_retries:
                logger.exception(f"Failed to fetch postings after {max_retries + 1} attempts")
                return []

            logger.warning(f"Attempt {attempt + 1} failed: {exc}, retrying with backoff...")
            backoff.wait()
    else:
        logger.error("Max retries reached for fetching job postings")
        return []

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
