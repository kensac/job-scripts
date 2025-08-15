from __future__ import annotations

import datetime
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TypedDict
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import dotenv
import gspread
import requests
from gspread.utils import ValueInputOption
from gspread.worksheet import Worksheet
from oauth2client.service_account import ServiceAccountCredentials  # type: ignore
from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


dotenv.load_dotenv()

# API Configuration
openai_api_key = os.environ.get("OPENAI_API_KEY")
openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None

# Logging Configuration
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
logging.basicConfig(
    level=logging.DEBUG, format=LOG_FORMAT, handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("job_tracker")

# Google Sheets Configuration
SHEET_ID: str = os.environ["SHEET_ID"]
SHEET_NAME: str = "Job Application Tracker"
FOUND_SOURCE_DEFAULT: str = "Direct Application"

# Job Filtering Configuration
EXCLUDED_LOCATIONS: Set[str] = {
    loc.lower()
    for loc in ["canada", "toronto", "montreal", "ontario", "london", "--------"]
}

INCLUDED_TERMS: Set[str] = {
    "Spring 2025",
    "Summer 2025",
    "Fall 2025",
    "Winter 2025",
    "Spring 2026",
    "Summer 2026",
    "Fall 2026",
    "Winter 2026",
    "Spring 2027",
    "Summer 2027",
    "Fall 2027",
    "Winter 2027",
    "Spring 2028",
    "Summer 2028",
    "Fall 2028",
    "Winter 2028",
    "Fall",
    "Summer",
    "Spring",
    "Winter",
}

# Date Configuration
FALLBACK_CUTOFF_DATE: str = "2025-03-01"
FALLBACK_CUTOFF_TS: int = int(
    datetime.datetime.fromisoformat(FALLBACK_CUTOFF_DATE).timestamp()
)

# File Configuration
JOB_LISTINGS_URL: str = os.environ["JOB_LISTINGS_URL"]
REJECTED_URLS_FILE: str = "ai_rejected_urls.json"

# URL Tracking Parameters to Remove
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "source",
    "campaign",
    "fbclid",
    "gclid",
    "_ga",
    "_gl",
    "mc_cid",
    "mc_eid",
    "hsCtaTracking",
    "hsa_",
}


@dataclass(frozen=True)
class JobPosting:
    company: str
    locations: List[str]
    title: str
    url: str
    terms: List[str]
    active: bool
    date_posted: int
    raw_url: str = ""


class ClearanceStats(TypedDict):
    jobs_checked_for_clearance: int
    jobs_filtered_by_clearance: int
    clearance_filtered_jobs: List[JobPosting]
    jobs_added_to_sheet: int


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
        jitter = delay * 0.25 * (2 * random.random() - 1)
        final_delay = max(0, delay + jitter)

        logger.debug(
            f"Exponential backoff: waiting {final_delay:.2f}s (attempt {self.attempt})"
        )
        time.sleep(final_delay)
        self.attempt += 1

    def reset(self) -> None:
        self.attempt = 0


def normalize_url(url: str) -> str:
    if not url:
        return url

    parsed = urlparse(url)
    query_params = parse_qs(parsed.query, keep_blank_values=True)

    filtered_params = {
        k: v
        for k, v in query_params.items()
        if not any(k.lower().startswith(param.lower()) for param in TRACKING_PARAMS)
        and k.lower() not in {p.lower() for p in TRACKING_PARAMS}
    }

    new_query = urlencode(filtered_params, doseq=True) if filtered_params else ""

    normalized = urlunparse(
        (
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            new_query,
            "",
        )
    )

    return normalized


def load_rejected_urls() -> Set[str]:
    rejected_file = Path(REJECTED_URLS_FILE)
    if not rejected_file.exists():
        return set()

    try:
        with open(rejected_file, "r") as f:
            data = json.load(f)
            return set(data.get("rejected_urls", []))
    except (json.JSONDecodeError, KeyError, Exception) as exc:
        logger.warning(f"Failed to load rejected URLs from {REJECTED_URLS_FILE}: {exc}")
        return set()


def save_rejected_urls(rejected_urls: Set[str]) -> None:
    try:
        with open(REJECTED_URLS_FILE, "w") as f:
            json.dump({"rejected_urls": list(rejected_urls)}, f, indent=2)
    except Exception as exc:
        logger.warning(f"Failed to save rejected URLs to {REJECTED_URLS_FILE}: {exc}")


def add_rejected_url(url: str) -> None:
    rejected_urls = load_rejected_urls()
    rejected_urls.add(url)
    save_rejected_urls(rejected_urls)


def is_url_rejected(url: str) -> bool:
    rejected_urls = load_rejected_urls()
    return url in rejected_urls


def extract_url_content(url: str, timeout: float = 10.0) -> Optional[str]:
    if not openai_client:
        return None

    # Try HTTP request first
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        response.raise_for_status()

        content = re.sub(r"<[^>]+>", " ", response.text)
        content = re.sub(r"\s+", " ", content).strip()

        if len(content) > 100:
            logger.debug(f"Extracted content via HTTP request: {len(content)} chars")
            return content

    except Exception as exc:
        logger.debug(f"HTTP request failed for {url}: {exc}")

    # Fallback to browser for JavaScript-heavy sites
    return _extract_with_browser(url, timeout)


def _extract_with_browser(url: str, timeout: float) -> Optional[str]:
    driver = None
    try:
        chrome_options = Options()
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        chrome_options.add_argument("--enable-javascript")
        chrome_options.add_argument("--disable-web-security")
        chrome_options.add_argument("--allow-running-insecure-content")

        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(max(timeout, 15))

        logger.info(f"Opening browser to load: {url}")
        driver.get(url)

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        logger.debug("Waiting for JavaScript to load content...")
        time.sleep(5)
        logger.debug("Allowing additional time for dynamic content...")
        time.sleep(5)

        body_element = driver.find_element(By.TAG_NAME, "body")
        content = body_element.text.strip()

        if content:
            logger.info(f"Extracted all body content: {len(content)} chars")
            return content
        else:
            logger.warning(
                f"No content found in body element for {url} - page may require manual interaction"
            )
            return None

    except Exception as exc:
        logger.debug(f"Browser extraction failed for {url}: {exc}")
        return None

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def check_security_clearance_requirement(content: str) -> bool:
    if not content or not openai_client:
        return False

    try:
        response = openai_client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {
                    "role": "system",
                    "content": """You are analyzing job postings to determine if they should be filtered out. 

                    Respond 'YES' to filter out jobs if you find ANY of these EXPLICIT conditions:
                    
                    SECURITY/CITIZENSHIP REQUIREMENTS:
                    - "Security Clearance" or "security clearance required"
                    - "U.S. Security Clearance" 
                    - "Secret Clearance", "Top Secret", "TS/SCI", "Public Trust"
                    - "Must be a U.S. citizen" or "U.S. citizenship required"
                    - "U.S. Person" as defined by export control regulations
                    
                    Respond 'YES' if ANY of these conditions are clearly stated.
                    Respond 'NO' only if NONE of these conditions are mentioned.
                    
                    IMPORTANT: Only respond 'YES' if you are absolutely certain. If content is unclear, vague, or insufficient, respond 'NO' to avoid false positives.
                    Do not make assumptions based on job title or company type.
                    Do not respond with anything other than 'YES' or 'NO'.
                    """,
                },
                {
                    "role": "user",
                    "content": f"Should this job posting be filtered out (security clearance/citizenship required)? Content: {content}",
                },
            ],
            max_completion_tokens=5000,
        )

        result = response.choices[0].message.content
        if result is None or result == "":
            logger.warning("AI returned null or empty response")
            return False

        result = result.strip().upper()
        logger.info(f"AI clearance check result: '{result}'")

        return "YES" in result

    except Exception as exc:
        logger.warning(f"AI security clearance check failed: {exc}")
        return False


def preprocess_job_posting(job: JobPosting) -> JobPosting:
    if not job.url or not job.active:
        return job

    if not openai_client:
        logger.debug("No OpenAI API key found - skipping AI preprocessing")
        return job

    if is_url_rejected(job.url):
        logger.info(
            f"FILTERED: Job already rejected by AI (cached) for {job.company} - {job.title} ({job.url})"
        )
        return JobPosting(
            company=job.company,
            locations=job.locations,
            title=job.title,
            url=job.url,
            terms=job.terms,
            active=False,
            date_posted=job.date_posted,
            raw_url=job.raw_url,
        )

    content = extract_url_content(job.url)
    if not content:
        logger.debug(f"Could not extract content from {job.url} - keeping job active")
        return job

    if len(content.strip()) < 100:
        logger.debug(f"Insufficient content from {job.url} - keeping job active")
        return job

    requires_clearance = check_security_clearance_requirement(content)

    if requires_clearance:
        logger.info(
            f"FILTERED: Job blocked by AI (security clearance/citizenship/closed) for {job.company} - {job.title} ({job.url})"
        )
        add_rejected_url(job.url)
        return JobPosting(
            company=job.company,
            locations=job.locations,
            title=job.title,
            url=job.url,
            terms=job.terms,
            active=False,
            date_posted=job.date_posted,
            raw_url=job.raw_url,
        )
    else:
        logger.debug(f"No issues detected by AI for {job.company} - {job.title}")

    return job


def fetch_job_postings(
    url: str, timeout: float = 10.0, max_retries: int = 3
) -> List[JobPosting]:
    backoff = ExponentialBackoff()

    for attempt in range(max_retries + 1):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_time = int(retry_after)
                    logger.warning(
                        f"Rate limited, waiting {wait_time}s as per Retry-After header"
                    )
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
                logger.error(
                    f"Failed to fetch postings after {max_retries + 1} attempts: {exc}"
                )
                return []

            logger.warning(
                f"Attempt {attempt + 1} failed: {exc}, retrying with backoff..."
            )
            backoff.wait()
    else:
        logger.error("Max retries reached for fetching job postings")
        return []

    postings: List[JobPosting] = []
    for entry in data:
        terms: List[str] = []
        if "terms" in entry:
            terms = entry["terms"] if isinstance(entry["terms"], list) else []
        elif "seasons" in entry:
            terms = entry["seasons"] if isinstance(entry["seasons"], list) else []

        raw_url = entry.get("url", "")
        normalized_url = normalize_url(raw_url)

        job = JobPosting(
            company=entry.get("company_name", ""),
            locations=(
                entry.get("locations", [])
                if isinstance(entry.get("locations"), list)
                else []
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


def is_location_excluded(location: str) -> bool:
    return any(loc in location.lower() for loc in EXCLUDED_LOCATIONS)


def is_terms_included(terms: List[str]) -> bool:
    return any(term in INCLUDED_TERMS for term in terms)


def filter_job_postings(
    postings: List[JobPosting], existing_urls: Set[str]
) -> List[JobPosting]:
    filtered: List[JobPosting] = []
    for job in postings:
        if not job.active:
            logger.debug(f"Skipping inactive: {job.company}")
            continue
        if is_location_excluded(" ".join(job.locations)):
            logger.debug(f"Skipping location: {job.locations}")
            continue
        if job.terms:
            if not is_terms_included(job.terms):
                logger.debug(f"Skipping terms: {job.terms}")
                continue
        else:
            if job.date_posted < FALLBACK_CUTOFF_TS:
                logger.debug(
                    f"Skipping no terms and date < {FALLBACK_CUTOFF_DATE}: {job.date_posted}"
                )
                continue
        if job.url in existing_urls or job.raw_url in existing_urls:
            logger.debug(f"Skipping duplicate URL: {job.url} (raw: {job.raw_url})")
            continue
        filtered.append(job)
    logger.info(f"{len(filtered)} new postings after filtering.")
    return filtered


def authenticate_gspread() -> gspread.client.Client:
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS_CUSTOM"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)  # type: ignore
    client = gspread.auth.authorize(creds)  # type: ignore
    logger.info("Authenticated with Google Sheets API.")
    return client


def get_existing_urls(sheet: Worksheet) -> Set[str]:
    rows: List[List[str]] = sheet.get_all_values()
    return {row[5] for row in rows if len(row) > 5}  # type: ignore


def write_to_sheet(sheet: Worksheet, jobs: List[JobPosting]) -> ClearanceStats:
    clearance_stats: ClearanceStats = {
        "jobs_checked_for_clearance": 0,
        "jobs_filtered_by_clearance": 0,
        "clearance_filtered_jobs": [],
        "jobs_added_to_sheet": 0,
    }

    if not jobs:
        return clearance_stats

    if openai_client:
        clearance_stats["jobs_checked_for_clearance"] = len(jobs)
        jobs_to_add: List[JobPosting] = []

        for job in jobs:
            final_job = preprocess_job_posting(job)
            if final_job.active:
                jobs_to_add.append(final_job)
            else:
                clearance_stats["jobs_filtered_by_clearance"] += 1
                clearance_stats["clearance_filtered_jobs"].append(job)
    else:
        logger.info("No OpenAI API key found - skipping AI security clearance checks")
        jobs_to_add = jobs

    if not jobs_to_add:
        return clearance_stats

    existing_rows = sheet.get_all_values()
    start_row = len(existing_rows) + 1
    rows_to_add: List[List[str]] = []

    for job in jobs_to_add:
        row: List[str] = [""] * 15
        row[1] = job.company
        row[3] = ", ".join(job.locations)
        row[4] = FOUND_SOURCE_DEFAULT
        row[5] = job.url
        row[6] = job.title
        row[7] = ", ".join(job.terms)
        rows_to_add.append(row)

    end_row = start_row + len(rows_to_add) - 1
    cell_range = f"A{start_row}:O{end_row}"
    try:
        sheet.update(
            rows_to_add, cell_range, value_input_option=ValueInputOption.user_entered
        )
        clearance_stats["jobs_added_to_sheet"] = len(rows_to_add)
    except Exception as exc:
        logger.error(f"Failed to update sheet: {exc}")
        clearance_stats["jobs_added_to_sheet"] = 0

    return clearance_stats


def summarize_filters(
    postings: List[JobPosting],
    existing_urls: Set[str],
    clearance_stats: ClearanceStats | None = None,
) -> None:
    excluded_locations: Set[str] = set()
    excluded_terms: Set[str] = set()
    summary: Dict[str, Any] = {
        "excluded_locations": excluded_locations,
        "excluded_terms": excluded_terms,
        "location_excluded_jobs": [],
        "term_excluded_jobs": [],
        "date_excluded_jobs": [],
        "duplicate_jobs": [],
        "passed_jobs": [],
        "inactive_jobs": [],
    }

    if clearance_stats is None:
        clearance_stats = {
            "jobs_checked_for_clearance": 0,
            "jobs_filtered_by_clearance": 0,
            "clearance_filtered_jobs": [],
            "jobs_added_to_sheet": 0,
        }

    inactive_jobs = summary["inactive_jobs"]
    location_excluded_jobs = summary["location_excluded_jobs"]
    term_excluded_jobs = summary["term_excluded_jobs"]
    date_excluded_jobs = summary["date_excluded_jobs"]
    duplicate_jobs = summary["duplicate_jobs"]
    passed_jobs = summary["passed_jobs"]

    for job in postings:
        if not job.active:
            inactive_jobs.append(job)
            continue

        if is_location_excluded(" ".join(job.locations)):
            location_excluded_jobs.append(job)
            for loc in job.locations:
                if is_location_excluded(loc):
                    excluded_locations.add(loc)
            continue

        if job.terms:
            if not is_terms_included(job.terms):
                term_excluded_jobs.append(job)
                for term in job.terms:
                    if term not in INCLUDED_TERMS:
                        excluded_terms.add(term)
                continue
        else:
            if job.date_posted < FALLBACK_CUTOFF_TS:
                date_excluded_jobs.append(job)
                continue

        if job.url in existing_urls or job.raw_url in existing_urls:
            duplicate_jobs.append(job)
            continue

        passed_jobs.append(job)

    logger.info("=" * 60)
    logger.info("FINAL FILTERING SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total Jobs Fetched: {len(postings)}")
    logger.info("")
    logger.info("EXCLUDED JOBS:")
    logger.info(f"  ├─ Inactive Jobs: {len(inactive_jobs)}")
    logger.info(f"  ├─ Location Excluded: {len(location_excluded_jobs)}")
    logger.info(f"  ├─ Term Excluded: {len(term_excluded_jobs)}")
    logger.info(f"  ├─ Date Excluded: {len(date_excluded_jobs)}")
    logger.info(f"  ├─ Duplicate Jobs: {len(duplicate_jobs)}")
    logger.info(
        f"  └─ AI Filtered (Clearance/Closed): {clearance_stats['jobs_filtered_by_clearance']}"
    )
    logger.info("")
    logger.info("RESULTS:")
    logger.info(f"  ├─ Jobs Passed Initial Filters: {len(passed_jobs)}")
    logger.info(
        f"  ├─ Jobs Checked for Security Clearance: {clearance_stats['jobs_checked_for_clearance']}"
    )
    logger.info(
        f"  └─ Jobs Added to Sheet: {clearance_stats.get('jobs_added_to_sheet', 0)}"
    )
    logger.info("")
    logger.info(f"Excluded Locations: {sorted(excluded_locations)}")
    logger.info(f"Excluded Terms: {sorted(excluded_terms)}")

    if clearance_stats["jobs_filtered_by_clearance"] > 0:
        logger.info("")
        logger.info("AI FILTERED JOBS (Security Clearance/Citizenship/Closed):")
        for job in clearance_stats["clearance_filtered_jobs"]:
            logger.info(f"  - {job.company}: {job.title}")
            logger.info(f"    URL: {job.url}")

    logger.info("=" * 60)


def main() -> None:
    client = authenticate_gspread()
    sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
    postings = fetch_job_postings(JOB_LISTINGS_URL)
    existing_urls = get_existing_urls(sheet)
    new_jobs = filter_job_postings(postings, existing_urls)

    clearance_stats = write_to_sheet(sheet, new_jobs)

    summarize_filters(postings, existing_urls, clearance_stats)


if __name__ == "__main__":
    main()
