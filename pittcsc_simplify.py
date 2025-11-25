from __future__ import annotations

import datetime
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, TypedDict
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import socket
import colorlog
import dotenv
import gspread
import requests
import wakepy
from gspread.utils import ValueInputOption
from gspread.worksheet import Worksheet
from oauth2client.service_account import ServiceAccountCredentials  # type: ignore
from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


dotenv.load_dotenv()

# API Configuration
openai_api_key = os.environ.get("OPENAI_API_KEY")
openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None

handler = colorlog.StreamHandler(sys.stdout)
handler.setFormatter(
    colorlog.ColoredFormatter(
        "%(asctime)s - %(log_color)s%(levelname)s%(reset)s - %(message)s",
        datefmt=None,
        reset=True,
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )
)
logging.basicConfig(level=logging.DEBUG, handlers=[handler])
logger = logging.getLogger("job_tracker")

# Google Sheets Configuration
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
AI_RESULTS_FILE: str = "ai_results.json"

# Content Extraction Configuration
BROWSER_PAGE_LOAD_TIMEOUT: float = 15.0
BROWSER_ELEMENT_WAIT_TIMEOUT: float = 10.0
BROWSER_CONTENT_WAIT: float = 5.0
BROWSER_TAB_POOL_SIZE: int = 10
BROWSER_STAGGER_DELAY: float = 1.0
MIN_CONTENT_LENGTH: int = 100
OPENAI_TIMEOUT: float = 120.0
OPENAI_MAX_RETRIES: int = 3
NETWORK_CHECK_TIMEOUT: float = 5.0
NETWORK_RETRY_DELAY: float = 10.0

# Sheet Configuration
SHEET_COLUMNS: int = 15

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
    jobs_passed: List[JobPosting]


class RunSummary(TypedDict):
    config_name: str
    total_fetched: int
    inactive: int
    location_excluded: int
    term_excluded: int
    date_excluded: int
    duplicates: int
    ai_filtered: int
    passed_initial: int
    checked_clearance: int
    added_to_sheet: int


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

    tracking_params_lower = {p.lower() for p in TRACKING_PARAMS}
    filtered_params = {
        k: v
        for k, v in query_params.items()
        if k.lower() not in tracking_params_lower
        and not any(k.lower().startswith(param) for param in tracking_params_lower)
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


def load_ai_results() -> Dict[str, Dict[str, str]]:
    results_file = Path(AI_RESULTS_FILE)
    if not results_file.exists():
        return {}

    try:
        with open(results_file, "r") as f:
            data = json.load(f)
            return data.get("ai_results", {})
    except (json.JSONDecodeError, KeyError, Exception) as exc:
        logger.warning(f"Failed to load AI results from {AI_RESULTS_FILE}: {exc}")
        return {}


def save_ai_results(ai_results: Dict[str, Dict[str, str]]) -> None:
    try:
        with open(AI_RESULTS_FILE, "w") as f:
            json.dump({"ai_results": ai_results}, f, indent=2)
    except Exception as exc:
        logger.warning(f"Failed to save AI results to {AI_RESULTS_FILE}: {exc}")


def add_ai_result(url: str, status: str, reason: str = "", check_type: str = "") -> None:
    ai_results = load_ai_results()
    ai_results[url] = {
        "status": status,
        "reason": reason,
        "check_type": check_type,
        "timestamp": datetime.datetime.now().isoformat()
    }
    save_ai_results(ai_results)


def get_ai_result(url: str) -> Optional[Dict[str, str]]:
    ai_results = load_ai_results()
    return ai_results.get(url)


def is_url_rejected(url: str) -> bool:
    result = get_ai_result(url)
    return result is not None and result.get("status") == "rejected"


def is_url_passed(url: str) -> bool:
    result = get_ai_result(url)
    return result is not None and result.get("status") == "passed"


def is_url_failed(url: str) -> bool:
    result = get_ai_result(url)
    return result is not None and result.get("status") == "failed"


@dataclass
class TabInfo:
    handle: str
    url: str
    load_time: float


class BrowserManager:
    def __init__(self) -> None:
        self.driver: Optional[webdriver.Chrome] = None
        self.tab_queue: List[TabInfo] = []
        self.pending_urls: List[str] = []
        self.completed_extractions: Dict[str, Optional[str]] = {}

    def _get_chrome_options(self) -> Options:
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        chrome_options.add_argument("--enable-javascript")
        chrome_options.add_argument("--disable-web-security")
        chrome_options.add_argument("--allow-running-insecure-content")
        return chrome_options

    def _ensure_driver(self) -> webdriver.Chrome:
        if self.driver is None:
            chrome_options = self._get_chrome_options()
            self.driver = webdriver.Chrome(options=chrome_options)
            self.driver.set_page_load_timeout(BROWSER_PAGE_LOAD_TIMEOUT)
            logger.debug("Created new browser instance")
        return self.driver

    def _recreate_driver(self) -> None:
        logger.warning("Recreating browser instance after failure")
        self.close()
        self.driver = None
        self.tab_queue = []
        self.pending_urls = []
        self.completed_extractions = {}

    def _start_loading_url(self, url: str) -> None:
        try:
            driver = self._ensure_driver()

            driver.execute_script("window.open('');")
            driver.switch_to.window(driver.window_handles[-1])

            logger.debug(f"Starting load for: {url}")
            driver.get(url)

            tab_info = TabInfo(
                handle=driver.current_window_handle,
                url=url,
                load_time=time.time()
            )
            self.tab_queue.append(tab_info)

        except Exception as exc:
            logger.debug(f"Failed to start loading {url}: {exc}")

    def add_to_pipeline(self, urls: List[str]) -> None:
        self.pending_urls.extend(urls)

    def fill_pipeline(self) -> None:
        while len(self.tab_queue) < BROWSER_TAB_POOL_SIZE and self.pending_urls:
            url = self.pending_urls.pop(0)
            self._start_loading_url(url)
            if self.pending_urls:
                time.sleep(BROWSER_STAGGER_DELAY)

    def extract_next(self) -> None:
        self.fill_pipeline()

        if not self.tab_queue:
            return

        tab_info = self.tab_queue.pop(0)

        try:
            driver = self._ensure_driver()

            elapsed = time.time() - tab_info.load_time
            remaining_wait = BROWSER_CONTENT_WAIT - elapsed
            if remaining_wait > 0:
                logger.debug(f"Waiting {remaining_wait:.1f}s for {tab_info.url}")
                time.sleep(remaining_wait)

            driver.switch_to.window(tab_info.handle)

            WebDriverWait(driver, BROWSER_ELEMENT_WAIT_TIMEOUT).until(
                lambda d: d.execute_script('return document.readyState') == 'complete'
            )

            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            body_element = driver.find_element(By.TAG_NAME, "body")
            content = body_element.text.strip()

            driver.close()
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])

            if content:
                logger.info(f"Extracted {len(content)} chars from {tab_info.url}")
                self.completed_extractions[tab_info.url] = content
            else:
                logger.warning(f"No content found for {tab_info.url}")
                self.completed_extractions[tab_info.url] = None

        except Exception as exc:
            logger.debug(f"Extraction failed for {tab_info.url}: {exc}")
            self.completed_extractions[tab_info.url] = None

    def get_content(self, url: str) -> Optional[str]:
        max_wait_iterations = 20
        wait_iterations = 0

        while url not in self.completed_extractions and wait_iterations < max_wait_iterations:
            if self.tab_queue or self.pending_urls:
                self.extract_next()
            wait_iterations += 1

        return self.completed_extractions.get(url)

    def close(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
                logger.debug("Closed browser instance")
            except Exception:
                pass
            finally:
                self.driver = None
                self.tab_queue = []
                self.pending_urls = []
                self.completed_extractions = {}


def extract_url_content(
    url: str, browser_manager: Optional[BrowserManager] = None
) -> Optional[str]:
    if not openai_client or not browser_manager:
        return None

    return browser_manager.get_content(url)


def check_if_job_closed(content: str, url: str = "") -> bool:
    if not content or not openai_client:
        return False

    if url:
        cached_result = get_ai_result(url)
        if cached_result and cached_result.get("check_type") == "closed":
            logger.info(f"Using cached closed status for {url}")
            return cached_result["status"] == "rejected"

    backoff = ExponentialBackoff(base_delay=2.0, max_delay=30.0)

    for attempt in range(OPENAI_MAX_RETRIES):
        try:
            response = openai_client.chat.completions.create(
                model="gpt-5-nano",
                messages=[
                    {
                        "role": "system",
                        "content": """You are checking if a job posting is closed or no longer available.

Respond 'YES' if you find ANY of these indicators:
- "Job posting no longer available"
- "This job is closed"
- "Position has been filled"
- "No longer accepting applications"
- "This posting has expired"
- "Application deadline has passed"
- "Position is no longer open"
- Similar phrases indicating the job is closed

Respond 'NO' if the job appears to be open and accepting applications.

IMPORTANT: Only respond 'YES' if you are absolutely certain the job is closed.
If unclear or no indication, respond 'NO'.
Do not respond with anything other than 'YES' or 'NO'.""",
                    },
                    {
                        "role": "user",
                        "content": f"Is this job posting closed? Content: {content}",
                    },
                ],
                max_completion_tokens=5000,
                timeout=OPENAI_TIMEOUT,
            )

            result = response.choices[0].message.content
            if result is None or result == "":
                logger.warning("AI returned null or empty response for closed check")
                return False

            result = result.strip().upper()

            if "YES" not in result and "NO" not in result:
                logger.warning(f"AI returned invalid response for closed check: '{result}'")
                return False

            logger.info(f"AI closed check result: '{result}'")

            is_closed = "YES" in result

            if url:
                if is_closed:
                    add_ai_result(url, "rejected", "job closed", "closed")
                else:
                    add_ai_result(url, "passed", "job open", "closed")

            return is_closed

        except Exception as exc:
            exc_str = str(exc).lower()
            if "timeout" in exc_str or "connection" in exc_str or "network" in exc_str:
                logger.warning(f"Network error during AI closed check: {exc}")
                wait_for_network()
                continue
            if attempt < OPENAI_MAX_RETRIES - 1:
                logger.warning(
                    f"AI closed check failed (attempt {attempt + 1}/{OPENAI_MAX_RETRIES}): {exc}"
                )
                backoff.wait()
            else:
                logger.warning(
                    f"AI closed check failed after {OPENAI_MAX_RETRIES} attempts: {exc}"
                )
                if url:
                    add_ai_result(url, "failed", f"AI check failed: {str(exc)[:100]}", "closed")
                return False

    if url:
        add_ai_result(url, "failed", "AI check failed: unknown error", "closed")
    return False


def check_security_clearance_requirement(content: str, url: str = "") -> bool:
    if not content or not openai_client:
        return False

    if url:
        cached_result = get_ai_result(url)
        if cached_result and cached_result.get("check_type") == "clearance":
            logger.info(f"Using cached clearance result for {url}: {cached_result['status']}")
            return cached_result["status"] == "rejected"

    backoff = ExponentialBackoff(base_delay=2.0, max_delay=30.0)

    for attempt in range(OPENAI_MAX_RETRIES):
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

            VISA SPONSORSHIP RESTRICTIONS:
            - "Will not sponsor" or "does not sponsor" (visa/work authorization)
            - "No sponsorship" or "sponsorship not available"
            - "Will not provide sponsorship" or "does not provide sponsorship"
            - "Cannot sponsor" or "unable to sponsor"
            - "Not sponsoring" or "no visa sponsorship"
            - "Will not sponsor now or in the future"
            - "Does not sponsor now or in the future"
            - "No current or future sponsorship"
            - "Will not sponsor for any visa type"
            - "Does not sponsor H1B", "No H1B sponsorship", "Will not sponsor H1B"
            - "Does not sponsor work visas" or "No work visa sponsorship"
            - "Must be authorized to work" combined with "no sponsorship"
            - "Authorized to work without sponsorship"
            - "No visa sponsorship provided"
            - "Sponsorship will not be provided"
            - Variations like "we do not sponsor", "company does not sponsor", "employer will not sponsor"

            F1 VISA RESTRICTIONS:
            - "F1 visa not eligible" or "F1 students not eligible"
            - "No F1 visa" or "F1 not accepted"
            - "F1 visa holders not considered"
            - "Not open to F1 students"
            - "F1 status not eligible"
            - "Does not accept F1 visa"
            - "F1 visa students cannot apply"
            - "Not available for F1 visa holders"

            Respond 'YES' if ANY of these conditions are clearly stated.
            Respond 'NO' only if NONE of these conditions are mentioned.

            IMPORTANT: Only respond 'YES' if you are absolutely certain the language explicitly states no sponsorship or F1 restrictions.
            If content is unclear, vague, or insufficient, respond 'NO' to avoid false positives.
            Do not make assumptions based on job title or company type.
            Do not respond with anything other than 'YES' or 'NO'.
            """,
                    },
                    {
                        "role": "user",
                        "content": f"Should this job posting be filtered out (security clearance/citizenship required OR no visa sponsorship OR F1 visa restrictions)? Content: {content}",
                    },
                ],
                max_completion_tokens=5000,
                timeout=OPENAI_TIMEOUT,
            )

            result = response.choices[0].message.content
            if result is None or result == "":
                logger.warning("AI returned null or empty response")
                return False

            result = result.strip().upper()

            if "YES" not in result and "NO" not in result:
                logger.warning(f"AI returned invalid response: '{result}'")
                return False

            logger.info(f"AI clearance check result: '{result}'")

            requires_clearance = "YES" in result

            if url:
                if requires_clearance:
                    add_ai_result(
                        url, "rejected", "security clearance/citizenship/visa restrictions", "clearance"
                    )
                else:
                    add_ai_result(url, "passed", "no security clearance required", "clearance")

            return requires_clearance

        except Exception as exc:
            exc_str = str(exc).lower()
            if "timeout" in exc_str or "connection" in exc_str or "network" in exc_str:
                logger.warning(f"Network error during AI call: {exc}")
                wait_for_network()
                continue
            if attempt < OPENAI_MAX_RETRIES - 1:
                logger.warning(
                    f"AI call failed (attempt {attempt + 1}/{OPENAI_MAX_RETRIES}): {exc}"
                )
                backoff.wait()
            else:
                logger.warning(
                    f"AI security clearance check failed after {OPENAI_MAX_RETRIES} attempts: {exc}"
                )
                if url:
                    add_ai_result(url, "failed", f"AI check failed: {str(exc)[:100]}", "clearance")
                return False

    if url:
        add_ai_result(url, "failed", "AI check failed: unknown error", "clearance")
    return False


def preprocess_job_posting(
    job: JobPosting, browser_manager: Optional[BrowserManager] = None
) -> JobPosting:
    if not job.url or not job.active:
        return job

    if not openai_client:
        logger.debug("No OpenAI API key found - skipping AI preprocessing")
        return job

    if is_url_rejected(job.url):
        logger.info(
            f"FILTERED: Job already rejected by AI (cached) for {job.company} - {job.title}"
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

    if is_url_passed(job.url):
        logger.info(
            f"Job already passed AI check (cached) for {job.company} - {job.title}"
        )
        return job

    content = extract_url_content(job.url, browser_manager)
    if not content:
        logger.debug(f"Could not extract content from {job.url} - keeping job active")
        add_ai_result(job.url, "failed", "failed to extract content", "extraction")
        return job

    if len(content.strip()) < MIN_CONTENT_LENGTH:
        logger.debug(f"Insufficient content from {job.url} - keeping job active")
        add_ai_result(job.url, "failed", f"insufficient content (only {len(content)} chars)", "extraction")
        return job

    is_closed = check_if_job_closed(content, job.url)
    if is_closed:
        logger.info(
            f"FILTERED: Job is closed for {job.company} - {job.title} ({job.url})"
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

    requires_clearance = check_security_clearance_requirement(content, job.url)

    if requires_clearance:
        logger.info(
            f"FILTERED: Job blocked by AI (security clearance/citizenship/closed) for {job.company} - {job.title} ({job.url})"
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


def filter_jobs_by_clearance(
    jobs: List[JobPosting], browser_manager: Optional[BrowserManager] = None
) -> ClearanceStats:
    clearance_stats: ClearanceStats = {
        "jobs_checked_for_clearance": 0,
        "jobs_filtered_by_clearance": 0,
        "clearance_filtered_jobs": [],
        "jobs_added_to_sheet": 0,
        "jobs_passed": [],
    }

    if not jobs:
        return clearance_stats

    if not openai_client:
        logger.info("No OpenAI API key found - skipping AI security clearance checks")
        clearance_stats["jobs_passed"] = jobs
        return clearance_stats

    clearance_stats["jobs_checked_for_clearance"] = len(jobs)
    jobs_passed: List[JobPosting] = []
    jobs_to_check: List[JobPosting] = []

    for job in jobs:
        if not job.url or not job.active:
            jobs_passed.append(job)
        elif is_url_rejected(job.url):
            logger.info(f"FILTERED: Job already rejected by AI (cached) for {job.company} - {job.title}")
            clearance_stats["jobs_filtered_by_clearance"] += 1
            clearance_stats["clearance_filtered_jobs"].append(job)
        elif is_url_passed(job.url):
            logger.info(f"Job already passed AI check (cached) for {job.company} - {job.title}")
            jobs_passed.append(job)
        else:
            jobs_to_check.append(job)

    if browser_manager and jobs_to_check:
        urls_to_check = [job.url for job in jobs_to_check]
        logger.info(f"Adding {len(urls_to_check)} URLs to pipeline")
        browser_manager.add_to_pipeline(urls_to_check)

        for job in jobs_to_check:
            wait_for_network()
            final_job = preprocess_job_posting(job, browser_manager)
            if final_job.active:
                jobs_passed.append(final_job)
            else:
                clearance_stats["jobs_filtered_by_clearance"] += 1
                clearance_stats["clearance_filtered_jobs"].append(job)

    clearance_stats["jobs_passed"] = jobs_passed
    return clearance_stats


def write_to_sheet(sheet: Worksheet, jobs: List[JobPosting]) -> int:
    if not jobs:
        return 0

    existing_rows: List[List[str]] = sheet.get_all_values()
    start_row: int = len(existing_rows) + 1
    rows_to_add: List[List[str]] = []
    jobs.sort(key=lambda x: x.date_posted)

    for job in jobs:
        row: List[str] = [""] * SHEET_COLUMNS
        row[1] = job.company
        row[3] = ", ".join(job.locations)
        row[4] = FOUND_SOURCE_DEFAULT
        row[5] = job.url
        row[6] = job.title
        row[7] = ", ".join(job.terms)
        rows_to_add.append(row)

    end_row: int = start_row + len(rows_to_add) - 1
    cell_range: str = f"A{start_row}:O{end_row}"
    try:
        sheet.update(
            rows_to_add, cell_range, value_input_option=ValueInputOption.user_entered
        )
        return len(rows_to_add)
    except Exception as exc:
        logger.error(f"Failed to update sheet: {exc}")
        return 0


def summarize_filters(
    postings: List[JobPosting],
    existing_urls: Set[str],
    clearance_stats: Optional[ClearanceStats] = None,
) -> RunSummary:
    excluded_locations: Set[str] = set()
    excluded_terms: Set[str] = set()

    inactive_jobs: List[JobPosting] = []
    location_excluded_jobs: List[JobPosting] = []
    term_excluded_jobs: List[JobPosting] = []
    date_excluded_jobs: List[JobPosting] = []
    duplicate_jobs: List[JobPosting] = []
    passed_jobs: List[JobPosting] = []

    if clearance_stats is None:
        clearance_stats = {
            "jobs_checked_for_clearance": 0,
            "jobs_filtered_by_clearance": 0,
            "clearance_filtered_jobs": [],
            "jobs_added_to_sheet": 0,
            "jobs_passed": [],
        }

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

    summary: RunSummary = {
        "config_name": "",
        "total_fetched": len(postings),
        "inactive": len(inactive_jobs),
        "location_excluded": len(location_excluded_jobs),
        "term_excluded": len(term_excluded_jobs),
        "date_excluded": len(date_excluded_jobs),
        "duplicates": len(duplicate_jobs),
        "ai_filtered": clearance_stats["jobs_filtered_by_clearance"],
        "passed_initial": len(passed_jobs),
        "checked_clearance": clearance_stats["jobs_checked_for_clearance"],
        "added_to_sheet": clearance_stats.get("jobs_added_to_sheet", 0),
    }

    return summary


def check_network() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=NETWORK_CHECK_TIMEOUT)
        return True
    except (socket.timeout, OSError):
        return False


def wait_for_network() -> None:
    if check_network():
        return
    while not check_network():
        logger.warning("Network unavailable, waiting to reconnect...")
        time.sleep(NETWORK_RETRY_DELAY)
    logger.info("Network connection restored")


def main() -> RunSummary | None:
    sheet_id = os.environ.get("SHEET_ID")
    job_listings_url = os.environ.get("JOB_LISTINGS_URL")

    if not sheet_id or not job_listings_url:
        logger.error("SHEET_ID and JOB_LISTINGS_URL environment variables must be set")
        raise ValueError("Missing required environment variables")

    with wakepy.keep.presenting():
        logger.info("System sleep prevention enabled")
        logger.info(f"Using SHEET_ID: {sheet_id}")
        logger.info(f"Using JOB_LISTINGS_URL: {job_listings_url}")
        browser_manager: BrowserManager = BrowserManager()
        try:
            wait_for_network()
            client: gspread.client.Client = authenticate_gspread()
            sheet: Worksheet = client.open_by_key(sheet_id).worksheet(SHEET_NAME)

            wait_for_network()
            postings: List[JobPosting] = fetch_job_postings(job_listings_url)
            existing_urls: Set[str] = get_existing_urls(sheet)
            new_jobs: List[JobPosting] = filter_job_postings(postings, existing_urls)

            clearance_stats: ClearanceStats = filter_jobs_by_clearance(
                new_jobs, browser_manager
            )

            wait_for_network()
            jobs_added: int = write_to_sheet(sheet, clearance_stats["jobs_passed"])
            clearance_stats["jobs_added_to_sheet"] = jobs_added

            summary = summarize_filters(postings, existing_urls, clearance_stats)
            summary["config_name"] = os.environ.get("CONFIG_NAME", "unknown")
            return summary
        except Exception as e:
            logger.error(f"Fatal error in main: {e}")
            raise
        finally:
            browser_manager.close()


if __name__ == "__main__":
    main()
