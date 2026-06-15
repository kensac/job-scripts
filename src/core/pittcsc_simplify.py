from __future__ import annotations

import asyncio
import datetime
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Literal, Optional, Set, TypedDict
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

import socket
import colorlog
import dotenv
import ftfy
import gspread
import requests
import wakepy
from gspread.utils import ValueInputOption
from gspread.worksheet import Worksheet
from oauth2client.service_account import ServiceAccountCredentials  # type: ignore
from openai import AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, Field
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from core.batch import BatchSpec, batch_enabled, run_responses_batch
from core.filters import compute_prompt_hash, get_filter_prompt, list_filter_names
from core.store import (
    add_ai_result,
    get_ai_result,
    get_all_custom_filter_jobs,
    get_all_failed_jobs,
    get_content,
    get_custom_result,
    get_latest,
    is_prelim_rejected,
    is_url_failed,
    is_url_passed,
    is_url_rejected,
    prefetch,
)


dotenv.load_dotenv()

# API Configuration
openai_api_key = os.environ.get("OPENAI_API_KEY")
openai_client = AsyncOpenAI(api_key=openai_api_key) if openai_api_key else None

# Custom Filtering Configuration
CUSTOM_FILTER_PROMPT = os.environ.get("CUSTOM_FILTER_PROMPT", "")

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
# FALLBACK_CUTOFF_DATE: str = "2025-03-01"
FALLBACK_CUTOFF_DATE = '2026-05-01'
FALLBACK_CUTOFF_TS: int = int(
    datetime.datetime.fromisoformat(FALLBACK_CUTOFF_DATE).timestamp()
)

# Content Extraction Configuration
BROWSER_PAGE_LOAD_TIMEOUT: float = 15.0
BROWSER_ELEMENT_WAIT_TIMEOUT: float = 10.0
BROWSER_CONTENT_WAIT: float = 7.5
MIN_CONTENT_LENGTH: int = 0
OPENAI_MODEL: str = "gpt-5-nano"
OPENAI_TIMEOUT: float = 120.0
OPENAI_MAX_RETRIES: int = 3
OPENAI_MAX_COMPLETION_TOKENS: int = 2500
OPENAI_REASONING_EFFORT: str = "low"
NETWORK_CHECK_TIMEOUT: float = 5.0
NETWORK_RETRY_DELAY: float = 10.0

# Concurrent Processing Configuration
MAX_CONCURRENT_JOBS: int = 5

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


def _usage_to_kwargs(response) -> Dict[str, Any]:
    usage = getattr(response, "usage", None)
    if not usage:
        return {}
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return {
        "prompt_tokens": getattr(usage, "input_tokens", None),
        "completion_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "cached_tokens": getattr(input_details, "cached_tokens", None),
        "reasoning_tokens": getattr(output_details, "reasoning_tokens", None),
    }


def _usage_dict_to_kwargs(usage: Optional[dict]) -> Dict[str, Any]:
    if not usage:
        return {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": input_details.get("cached_tokens"),
        "reasoning_tokens": output_details.get("reasoning_tokens"),
    }


class JobClosedResponse(BaseModel):
    is_closed: bool = Field(description="Whether the job posting is closed or no longer available")
    reason: Optional[str] = Field(None, description="Brief explanation if job is closed")


class ClearanceRequirementResponse(BaseModel):
    requires_clearance_or_restrictions: bool = Field(
        description="Whether job requires security clearance, citizenship, or has visa/sponsorship restrictions"
    )
    restriction_type: Optional[Literal["security_clearance", "citizenship", "visa_sponsorship", "f1_restriction"]] = Field(
        None, description="Type of restriction if any"
    )
    reason: Optional[str] = Field(None, description="Brief explanation of the restriction")


class CustomFilterResponse(BaseModel):
    should_filter: bool = Field(description="Whether the job should be filtered out based on custom criteria")
    reason: Optional[str] = Field(None, description="Brief explanation why job was filtered or kept")


def get_chrome_options() -> Options:
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


def extract_url_content(url: str) -> Optional[str]:
    if not openai_client:
        return None

    driver = None
    try:
        chrome_options = get_chrome_options()
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(BROWSER_PAGE_LOAD_TIMEOUT)

        logger.debug(f"Starting load for: {url}")
        driver.get(url)

        WebDriverWait(driver, BROWSER_ELEMENT_WAIT_TIMEOUT).until(
            lambda d: d.execute_script('return document.readyState') == 'complete' # type: ignore
        )

        time.sleep(BROWSER_CONTENT_WAIT)

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);") # type: ignore
        
        body_element = driver.find_element(By.TAG_NAME, "body")
        content = ftfy.fix_text(body_element.text).strip()

        if content:
            logger.info(f"Extracted {len(content)} chars from {url}")
            return content
        else:
            logger.warning(f"No content found for {url}")
            return None

    except Exception as exc:
        logger.debug(f"Extraction failed for {url}: {exc}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


CLOSED_INSTRUCTIONS = """Decide if a job posting is closed or still accepting applications.

Set is_closed=true ONLY on explicit signals: "no longer available/accepting applications", "position filled", "posting expired", "deadline passed", "no longer open", "404"/"page not found", or an error page shown instead of job details.
Otherwise is_closed=false. If job details are present or it is ambiguous, default to false (avoid false positives).

reason: <=15 words; quote the exact closed phrase when found. Be decisive, no hedging."""

CLEARANCE_INSTRUCTIONS = """Flag job postings that disqualify international candidates via explicit restrictions.

Set requires_clearance_or_restrictions=true ONLY for explicit restrictions, and set restriction_type:
- security_clearance: required clearance or citizenship ("US citizen required", "US Person", "Secret/Top Secret/TS-SCI/Public Trust").
- visa_sponsorship: "will not / does not sponsor", "no H1B sponsorship", "must be authorized to work without sponsorship".
- f1_restriction: "F1 not eligible/accepted/considered".

Do NOT flag: preferences ("clearance preferred"), sponsorship offered, application questions ("will you require sponsorship?"), or absent mentions. When in doubt, set false (prefer false negatives). If several apply, pick the most restrictive type.

reason: <=20 words, quote the phrase."""

def build_custom_instructions(prompt: str) -> str:
    if not prompt:
        return ""
    return f"""Evaluate a job against the user criteria below and decide whether to filter it out.

<user_criteria>
{prompt}
</user_criteria>

should_filter=true only if the job clearly violates the criteria; false if it matches or is ambiguous (prefer false negatives, do not lose good roles).

reason: <=25 words citing the deciding factor (company/role/skills)."""


CUSTOM_INSTRUCTIONS = build_custom_instructions(CUSTOM_FILTER_PROMPT)

# The custom filter the current run applies. Defaults to the .env prompt;
# --apply-filter swaps it so the whole pipeline (new + cached jobs) uses it.
ACTIVE_FILTER_NAME = "default"
ACTIVE_CUSTOM_INSTRUCTIONS = CUSTOM_INSTRUCTIONS


def set_active_filter(name: str) -> bool:
    global ACTIVE_FILTER_NAME, ACTIVE_CUSTOM_INSTRUCTIONS
    prompt = get_filter_prompt(name)
    if not prompt:
        return False
    ACTIVE_FILTER_NAME = name
    ACTIVE_CUSTOM_INSTRUCTIONS = build_custom_instructions(prompt)
    return True


def _build_custom_input(content: str, job_title: str, company: str) -> str:
    return f"Company: {company}\nJob Title: {job_title}\n\nJob Content:\n{content}"


async def check_if_job_closed(
    content: str, url: str = "", job_title: str = "", company: str = ""
) -> bool:
    if not content or not openai_client:
        return False

    if url:
        cached_result = get_latest(url, "closed")
        if cached_result:
            logger.info(f"Using cached closed status for {url}")
            return cached_result["status"] == "rejected"

    instructions = CLOSED_INSTRUCTIONS

    backoff = ExponentialBackoff(base_delay=2.0, max_delay=30.0)

    for attempt in range(OPENAI_MAX_RETRIES):
        try:
            start = time.monotonic()
            response = await openai_client.responses.parse(
                model=OPENAI_MODEL,
                instructions=instructions,
                input=content,
                text_format=JobClosedResponse,
                reasoning={"effort": OPENAI_REASONING_EFFORT},
                max_output_tokens=OPENAI_MAX_COMPLETION_TOKENS,
                store=False,
                timeout=OPENAI_TIMEOUT,
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            parsed = response.output_parsed
            if not parsed:
                logger.warning("AI returned no parsed response for closed check")
                return False

            logger.info(f"AI closed check: is_closed={parsed.is_closed}, reason={parsed.reason}")

            if url:
                record: Dict[str, Any] = dict(
                    model=OPENAI_MODEL,
                    reasoning_effort=OPENAI_REASONING_EFFORT,
                    company=company,
                    job_title=job_title,
                    instructions=instructions,
                    input_content=content,
                    parsed_json=parsed.model_dump_json(),
                    duration_ms=duration_ms,
                    **_usage_to_kwargs(response),
                )
                if parsed.is_closed:
                    add_ai_result(url, "rejected", parsed.reason or "job closed", "closed", **record)
                else:
                    add_ai_result(url, "passed", parsed.reason or "job open", "closed", **record)

            return parsed.is_closed

        except Exception as exc:
            exc_str = str(exc).lower()
            if "timeout" in exc_str or "connection" in exc_str or "network" in exc_str:
                logger.warning(f"Network error during AI closed check: {exc}")
                await asyncio.to_thread(wait_for_network)
                continue
            if attempt < OPENAI_MAX_RETRIES - 1:
                logger.warning(
                    f"AI closed check failed (attempt {attempt + 1}/{OPENAI_MAX_RETRIES}): {exc}"
                )
                await asyncio.to_thread(backoff.wait)
            else:
                logger.warning(
                    f"AI closed check failed after {OPENAI_MAX_RETRIES} attempts: {exc}"
                )
                if url:
                    add_ai_result(
                        url, "failed", f"AI check failed: {str(exc)[:100]}", "closed",
                        model=OPENAI_MODEL, reasoning_effort=OPENAI_REASONING_EFFORT,
                        company=company, job_title=job_title,
                        instructions=instructions, input_content=content, error=str(exc),
                    )
                return False

    if url:
        add_ai_result(
            url, "failed", "AI check failed: unknown error", "closed",
            model=OPENAI_MODEL, reasoning_effort=OPENAI_REASONING_EFFORT,
            company=company, job_title=job_title,
            instructions=instructions, input_content=content,
        )
    return False


async def check_security_clearance_requirement(
    content: str, url: str = "", job_title: str = "", company: str = ""
) -> bool:
    if not content or not openai_client:
        return False

    if url:
        cached_result = get_latest(url, "clearance")
        if cached_result:
            logger.info(f"Using cached clearance result for {url}: {cached_result['status']}")
            return cached_result["status"] == "rejected"

    instructions = CLEARANCE_INSTRUCTIONS

    backoff = ExponentialBackoff(base_delay=2.0, max_delay=30.0)

    for attempt in range(OPENAI_MAX_RETRIES):
        try:
            start = time.monotonic()
            response = await openai_client.responses.parse(
                model=OPENAI_MODEL,
                instructions=instructions,
                input=content,
                text_format=ClearanceRequirementResponse,
                reasoning={"effort": OPENAI_REASONING_EFFORT},
                max_output_tokens=OPENAI_MAX_COMPLETION_TOKENS,
                store=False,
                timeout=OPENAI_TIMEOUT,
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            parsed = response.output_parsed
            if not parsed:
                logger.warning("AI returned no parsed response for clearance check")
                return False

            logger.info(
                f"AI clearance check: requires={parsed.requires_clearance_or_restrictions}, "
                f"type={parsed.restriction_type}, reason={parsed.reason}"
            )

            if url:
                record: Dict[str, Any] = dict(
                    model=OPENAI_MODEL,
                    reasoning_effort=OPENAI_REASONING_EFFORT,
                    company=company,
                    job_title=job_title,
                    instructions=instructions,
                    input_content=content,
                    parsed_json=parsed.model_dump_json(),
                    duration_ms=duration_ms,
                    **_usage_to_kwargs(response),
                )
                if parsed.requires_clearance_or_restrictions:
                    add_ai_result(
                        url, "rejected",
                        parsed.reason or f"{parsed.restriction_type} restriction",
                        "clearance",
                        **record,
                    )
                else:
                    add_ai_result(
                        url, "passed", parsed.reason or "no restrictions", "clearance",
                        **record,
                    )

            return parsed.requires_clearance_or_restrictions

        except Exception as exc:
            exc_str = str(exc).lower()
            if "timeout" in exc_str or "connection" in exc_str or "network" in exc_str:
                logger.warning(f"Network error during AI call: {exc}")
                await asyncio.to_thread(wait_for_network)
                continue
            if attempt < OPENAI_MAX_RETRIES - 1:
                logger.warning(
                    f"AI call failed (attempt {attempt + 1}/{OPENAI_MAX_RETRIES}): {exc}"
                )
                await asyncio.to_thread(backoff.wait)
            else:
                logger.warning(
                    f"AI security clearance check failed after {OPENAI_MAX_RETRIES} attempts: {exc}"
                )
                if url:
                    add_ai_result(
                        url, "failed", f"AI check failed: {str(exc)[:100]}", "clearance",
                        model=OPENAI_MODEL, reasoning_effort=OPENAI_REASONING_EFFORT,
                        company=company, job_title=job_title,
                        instructions=instructions, input_content=content, error=str(exc),
                    )
                return False

    if url:
        add_ai_result(
            url, "failed", "AI check failed: unknown error", "clearance",
            model=OPENAI_MODEL, reasoning_effort=OPENAI_REASONING_EFFORT,
            company=company, job_title=job_title,
            instructions=instructions, input_content=content,
        )
    return False


async def check_custom_filter(
    content: str,
    url: str = "",
    job_title: str = "",
    company: str = "",
    instructions: Optional[str] = None,
    filter_name: Optional[str] = None,
) -> bool:
    instructions = instructions if instructions is not None else ACTIVE_CUSTOM_INSTRUCTIONS
    filter_name = filter_name if filter_name is not None else ACTIVE_FILTER_NAME
    if not content or not openai_client or not instructions:
        return False

    phash = compute_prompt_hash(instructions)

    if url:
        cached_result = get_custom_result(url, phash)
        if cached_result:
            logger.info(f"Using cached custom filter result for {url}: {cached_result['status']}")
            return cached_result["status"] == "rejected"

    input_text = _build_custom_input(content, job_title, company)

    backoff = ExponentialBackoff(base_delay=2.0, max_delay=30.0)

    for attempt in range(OPENAI_MAX_RETRIES):
        try:
            start = time.monotonic()
            response = await openai_client.responses.parse(
                model=OPENAI_MODEL,
                instructions=instructions,
                input=input_text,
                text_format=CustomFilterResponse,
                reasoning={"effort": OPENAI_REASONING_EFFORT},
                max_output_tokens=OPENAI_MAX_COMPLETION_TOKENS,
                store=False,
                timeout=OPENAI_TIMEOUT,
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            parsed = response.output_parsed
            if not parsed:
                logger.warning("AI returned no parsed response for custom filter")
                return False

            logger.info(
                f"AI custom filter: should_filter={parsed.should_filter}, reason={parsed.reason}"
            )

            if url:
                record: Dict[str, Any] = dict(
                    model=OPENAI_MODEL,
                    reasoning_effort=OPENAI_REASONING_EFFORT,
                    filter_name=filter_name,
                    prompt_hash=phash,
                    company=company,
                    job_title=job_title,
                    instructions=instructions,
                    input_content=input_text,
                    parsed_json=parsed.model_dump_json(),
                    duration_ms=duration_ms,
                    **_usage_to_kwargs(response),
                )
                if parsed.should_filter:
                    add_ai_result(url, "rejected", parsed.reason or "custom filter criteria not met", "custom", **record)
                else:
                    add_ai_result(url, "passed", parsed.reason or "custom filter criteria met", "custom", **record)

            return parsed.should_filter

        except Exception as exc:
            exc_str = str(exc).lower()
            if "timeout" in exc_str or "connection" in exc_str or "network" in exc_str:
                logger.warning(f"Network error during AI custom filter: {exc}")
                await asyncio.to_thread(wait_for_network)
                continue
            if attempt < OPENAI_MAX_RETRIES - 1:
                logger.warning(
                    f"AI custom filter failed (attempt {attempt + 1}/{OPENAI_MAX_RETRIES}): {exc}"
                )
                await asyncio.to_thread(backoff.wait)
            else:
                logger.warning(
                    f"AI custom filter failed after {OPENAI_MAX_RETRIES} attempts: {exc}"
                )
                if url:
                    add_ai_result(
                        url, "failed", f"AI custom filter failed: {str(exc)[:100]}", "custom",
                        model=OPENAI_MODEL, reasoning_effort=OPENAI_REASONING_EFFORT,
                        filter_name=filter_name, prompt_hash=phash,
                        company=company, job_title=job_title,
                        instructions=instructions, input_content=input_text, error=str(exc),
                    )
                return False

    if url:
        add_ai_result(
            url, "failed", "AI custom filter failed: unknown error", "custom",
            model=OPENAI_MODEL, reasoning_effort=OPENAI_REASONING_EFFORT,
            filter_name=filter_name, prompt_hash=phash,
            company=company, job_title=job_title,
            instructions=instructions, input_content=input_text,
        )
    return False


async def preprocess_job_posting(
    job: JobPosting
) -> JobPosting:
    if not job.url or not job.active:
        return job

    if not openai_client:
        logger.debug("No OpenAI API key found - skipping AI preprocessing")
        return job

    if is_prelim_rejected(job.url):
        logger.info(
            f"FILTERED: Job already rejected by closed/clearance (cached) for {job.company} - {job.title}"
        )
        return replace(job, active=False)

    content = await asyncio.to_thread(get_content, job.url)
    if not content:
        content = await asyncio.to_thread(extract_url_content, job.url)
    if not content:
        logger.debug(f"Could not extract content from {job.url} - keeping job active")
        add_ai_result(job.url, "failed", "failed to extract content", "extraction")
        return job

    if len(content.strip()) < MIN_CONTENT_LENGTH:
        logger.debug(f"Insufficient content from {job.url} - keeping job active")
        add_ai_result(job.url, "failed", f"insufficient content (only {len(content)} chars)", "extraction")
        return job

    is_closed = await check_if_job_closed(content, job.url, job.title, job.company)
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

    requires_clearance = await check_security_clearance_requirement(
        content, job.url, job.title, job.company
    )

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

    if ACTIVE_CUSTOM_INSTRUCTIONS:
        should_filter_custom = await check_custom_filter(content, job.url, job.title, job.company)
        if should_filter_custom:
            logger.info(
                f"FILTERED: Job blocked by custom filter for {job.company} - {job.title} ({job.url})"
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

    logger.debug(f"No issues detected by AI for {job.company} - {job.title}")

    return job


async def retry_failed_job(url: str, check_type: str) -> bool:
    if not openai_client:
        logger.warning("No OpenAI API key found - cannot retry failed jobs")
        return False

    logger.info(f"Retrying failed job: {url} (check_type: {check_type})")

    try:
        await asyncio.to_thread(wait_for_network)
        content = await asyncio.to_thread(extract_url_content, url)

        if not content:
            logger.warning(f"Failed to extract content for {url}")
            add_ai_result(url, "failed", "Content extraction failed", check_type)
            return False

        if check_type == "closed":
            is_closed = await check_if_job_closed(content, url)
            logger.info(f"Retry result for {url}: is_closed={is_closed}")
            return True

        elif check_type == "clearance":
            requires_clearance = await check_security_clearance_requirement(content, url)
            logger.info(f"Retry result for {url}: requires_clearance={requires_clearance}")
            return True

        elif check_type == "custom":
            if not CUSTOM_FILTER_PROMPT:
                logger.warning(f"Cannot retry custom filter for {url}: CUSTOM_FILTER_PROMPT not set")
                return False
            should_filter = await check_custom_filter(content, url, "", "")
            logger.info(f"Retry result for {url}: should_filter={should_filter}")
            return True

        else:
            logger.warning(f"Unknown check_type for retry: {check_type}")
            return False

    except Exception as exc:
        logger.error(f"Error retrying failed job {url}: {exc}")
        add_ai_result(url, "failed", f"Retry failed: {str(exc)[:100]}", check_type)
        return False


async def reevaluate_custom_filter(url: str, job_title: str = "", company: str = "") -> Optional[bool]:
    if not openai_client or not CUSTOM_FILTER_PROMPT:
        logger.warning("No OpenAI API key or CUSTOM_FILTER_PROMPT found - cannot reevaluate")
        return None

    logger.info(f"Reevaluating custom filter for: {url}")

    try:
        await asyncio.to_thread(wait_for_network)
        content = await asyncio.to_thread(extract_url_content, url)

        if not content:
            logger.warning(f"Failed to extract content for {url}")
            add_ai_result(url, "failed", "Content extraction failed", "custom")
            return None

        should_filter = await check_custom_filter(content, url, job_title, company)
        logger.info(f"Reevaluation result for {url}: should_filter={should_filter}")
        return not should_filter

    except Exception as exc:
        logger.error(f"Error reevaluating custom filter for {url}: {exc}")
        add_ai_result(url, "failed", f"Reevaluation failed: {str(exc)[:100]}", "custom")
        return None


async def retry_all_failed_jobs() -> Dict[str, int]:
    failed_jobs = get_all_failed_jobs()

    if not failed_jobs:
        logger.info("No failed jobs found in ai_results.db")
        return {"total": 0, "success": 0, "failed": 0}

    logger.info(f"Found {len(failed_jobs)} failed jobs to retry")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    success_count = 0
    failed_count = 0

    async def retry_with_semaphore(job: Dict[str, str]) -> bool:
        nonlocal success_count, failed_count
        async with semaphore:
            await asyncio.to_thread(wait_for_network)
            success = await retry_failed_job(job["url"], job.get("check_type", "closed"))
            if success:
                success_count += 1
            else:
                failed_count += 1
            return success

    logger.info(f"Processing {len(failed_jobs)} failed jobs concurrently (max {MAX_CONCURRENT_JOBS} at a time)")
    await asyncio.gather(*[retry_with_semaphore(job) for job in failed_jobs])

    logger.info(f"Retry complete: {success_count} succeeded, {failed_count} failed")
    return {"total": len(failed_jobs), "success": success_count, "failed": failed_count}


async def reevaluate_all_custom_filtered_jobs(sheet_id: str, job_listings_url: str) -> Dict[str, int]:
    custom_jobs = get_all_custom_filter_jobs()

    if not custom_jobs:
        logger.info("No custom filter jobs found in ai_results.db")
        return {"total": 0, "reevaluated": 0, "now_passed": 0, "added_to_sheet": 0}

    if not CUSTOM_FILTER_PROMPT:
        logger.error("CUSTOM_FILTER_PROMPT not set - cannot reevaluate")
        return {"total": len(custom_jobs), "reevaluated": 0, "now_passed": 0, "added_to_sheet": 0}

    logger.info(f"Found {len(custom_jobs)} custom filter jobs to reevaluate")

    await asyncio.to_thread(wait_for_network)
    client = await asyncio.to_thread(authenticate_gspread)
    sheet = client.open_by_key(sheet_id).worksheet(SHEET_NAME)

    await asyncio.to_thread(wait_for_network)
    all_postings = await asyncio.to_thread(fetch_job_postings, job_listings_url)
    existing_sheet_urls = await asyncio.to_thread(get_existing_urls, sheet)

    url_to_posting = {job.url: job for job in all_postings}
    custom_job_urls = {job["url"] for job in custom_jobs}
    jobs_to_reevaluate = [url_to_posting[url] for url in custom_job_urls if url in url_to_posting]

    logger.info(f"Found {len(jobs_to_reevaluate)} jobs in current listings that need reevaluation")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    reevaluated_count = 0
    now_passed_count = 0
    jobs_to_add = []

    async def reevaluate_with_semaphore(job: JobPosting) -> Optional[JobPosting]:
        nonlocal reevaluated_count, now_passed_count
        async with semaphore:
            await asyncio.to_thread(wait_for_network)
            passed = await reevaluate_custom_filter(job.url, job.title, job.company)
            reevaluated_count += 1

            if passed:
                now_passed_count += 1
                if job.url not in existing_sheet_urls:
                    return job
            return None

    logger.info(f"Reevaluating {len(jobs_to_reevaluate)} jobs concurrently (max {MAX_CONCURRENT_JOBS} at a time)")
    results = await asyncio.gather(*[reevaluate_with_semaphore(job) for job in jobs_to_reevaluate])

    jobs_to_add = [job for job in results if job is not None]

    if jobs_to_add:
        logger.info(f"Adding {len(jobs_to_add)} newly passed jobs to sheet")
        await asyncio.to_thread(wait_for_network)
        added_count = await asyncio.to_thread(write_to_sheet, sheet, jobs_to_add)
    else:
        added_count = 0

    logger.info(f"Reevaluation complete: {reevaluated_count} jobs reevaluated, {now_passed_count} now pass, {added_count} added to sheet")
    return {
        "total": len(custom_jobs),
        "reevaluated": reevaluated_count,
        "now_passed": now_passed_count,
        "added_to_sheet": added_count
    }


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


def fetch_airtable_postings(
    url: str, timeout: float = 30.0, max_retries: int = 3
) -> List[JobPosting]:
    backoff = ExponentialBackoff()
    data: Optional[dict] = None

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
                logger.error(
                    f"Failed to fetch Airtable postings after {max_retries + 1} attempts: {exc}"
                )
                return []
            logger.warning(
                f"Airtable attempt {attempt + 1} failed: {exc}, retrying with backoff..."
            )
            backoff.wait()

    table = (data or {}).get("table", {})
    name_by_id = {c["id"]: c.get("name", "") for c in table.get("columns", [])}

    postings: List[JobPosting] = []
    for row in table.get("rows", []):
        values = {
            name_by_id.get(cid, ""): val
            for cid, val in row.get("cellValuesByColumnId", {}).items()
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


def fetch_job_postings(
    url: str, timeout: float = 10.0, max_retries: int = 3
) -> List[JobPosting]:
    if "airtable.com" in urlparse(url).netloc:
        return fetch_airtable_postings(url, max_retries=max_retries)

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


async def _extract_contents(jobs: List[JobPosting]) -> Dict[str, Optional[str]]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

    async def _one(job: JobPosting):
        async with semaphore:
            cached = await asyncio.to_thread(get_content, job.url)
            if cached:
                return job.url, cached
            await asyncio.to_thread(wait_for_network)
            return job.url, await asyncio.to_thread(extract_url_content, job.url)

    contents: Dict[str, Optional[str]] = {}
    for url, content in await asyncio.gather(*[_one(job) for job in jobs]):
        contents[url] = content
    return contents


async def _run_batch_stage(
    jobs: List[JobPosting],
    active: Dict[str, bool],
    contents: Dict[str, Optional[str]],
    check_type: str,
    model_cls,
    instructions_for,
    input_for,
    reject_of,
    reason_of,
) -> None:
    specs: List[BatchSpec] = []
    ctx: Dict[str, tuple] = {}
    for job in jobs:
        if not active.get(job.url):
            continue
        content = contents.get(job.url)
        if not content:
            continue
        instructions = instructions_for(job)
        if check_type == "custom":
            cached = get_custom_result(job.url, compute_prompt_hash(instructions))
        else:
            cached = get_latest(job.url, check_type)
        if cached:
            if cached["status"] == "rejected":
                active[job.url] = False
            continue
        input_text = input_for(job, content)
        specs.append(
            BatchSpec(job.url, instructions, input_text, model_cls.__name__, to_strict_json_schema(model_cls))
        )
        ctx[job.url] = (job, instructions, input_text)

    if not specs:
        return

    logger.info(f"Batch stage '{check_type}': {len(specs)} jobs to check")
    results = await run_responses_batch(
        specs,
        model=OPENAI_MODEL,
        reasoning_effort=OPENAI_REASONING_EFFORT,
        max_output_tokens=OPENAI_MAX_COMPLETION_TOKENS,
    )

    for url, (job, instructions, input_text) in ctx.items():
        res = results.get(url)
        if res is None or res.error or not res.text:
            err = res.error if res else "no result"
            logger.warning(f"Batch {check_type} failed for {url}: {err}")
            add_ai_result(
                url, "failed", f"batch {check_type} failed: {str(err)[:100]}", check_type,
                model=OPENAI_MODEL, reasoning_effort=OPENAI_REASONING_EFFORT,
                instructions=instructions, input_content=input_text, error=str(err),
            )
            continue
        try:
            parsed = model_cls.model_validate_json(res.text)
        except Exception as exc:
            logger.warning(f"Batch {check_type} parse failed for {url}: {exc}")
            add_ai_result(
                url, "failed", f"batch {check_type} parse failed: {str(exc)[:100]}", check_type,
                model=OPENAI_MODEL, reasoning_effort=OPENAI_REASONING_EFFORT,
                instructions=instructions, input_content=input_text, error=str(exc),
            )
            continue
        rejected = reject_of(parsed)
        extra = (
            {"filter_name": ACTIVE_FILTER_NAME, "prompt_hash": compute_prompt_hash(instructions)}
            if check_type == "custom"
            else {}
        )
        add_ai_result(
            url, "rejected" if rejected else "passed", reason_of(parsed), check_type,
            model=OPENAI_MODEL, reasoning_effort=OPENAI_REASONING_EFFORT,
            company=job.company, job_title=job.title,
            instructions=instructions, input_content=input_text,
            parsed_json=parsed.model_dump_json(), **extra, **_usage_dict_to_kwargs(res.usage),
        )
        if rejected:
            logger.info(f"FILTERED (batch/{check_type}): {job.company} - {job.title} ({url})")
            active[url] = False


async def _process_jobs_batched(jobs: List[JobPosting]) -> List[JobPosting]:
    logger.info(f"BATCH MODE: processing {len(jobs)} jobs via OpenAI Batch API")
    contents = await _extract_contents(jobs)

    active: Dict[str, bool] = {}
    for job in jobs:
        if not contents.get(job.url):
            add_ai_result(job.url, "failed", "failed to extract content", "extraction")
            logger.debug(f"Could not extract content from {job.url} - keeping job active")
        active[job.url] = True

    await _run_batch_stage(
        jobs, active, contents, "closed", JobClosedResponse,
        lambda job: CLOSED_INSTRUCTIONS,
        lambda job, content: content,
        lambda p: p.is_closed,
        lambda p: p.reason or ("job closed" if p.is_closed else "job open"),
    )
    await _run_batch_stage(
        jobs, active, contents, "clearance", ClearanceRequirementResponse,
        lambda job: CLEARANCE_INSTRUCTIONS,
        lambda job, content: content,
        lambda p: p.requires_clearance_or_restrictions,
        lambda p: p.reason
        or (f"{p.restriction_type} restriction" if p.requires_clearance_or_restrictions else "no restrictions"),
    )
    if ACTIVE_CUSTOM_INSTRUCTIONS:
        await _run_batch_stage(
            jobs, active, contents, "custom", CustomFilterResponse,
            lambda job: ACTIVE_CUSTOM_INSTRUCTIONS,
            lambda job, content: _build_custom_input(content, job.title, job.company),
            lambda p: p.should_filter,
            lambda p: p.reason
            or ("custom filter criteria not met" if p.should_filter else "custom filter criteria met"),
        )

    return [job if active.get(job.url) else replace(job, active=False) for job in jobs]


async def filter_jobs_by_clearance(
    jobs: List[JobPosting]
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

    prompt_hashes = (
        (compute_prompt_hash(ACTIVE_CUSTOM_INSTRUCTIONS),)
        if ACTIVE_CUSTOM_INSTRUCTIONS
        else ()
    )
    await asyncio.to_thread(
        prefetch, [job.url for job in jobs], prompt_hashes=prompt_hashes
    )

    for job in jobs:
        if not job.url or not job.active:
            jobs_passed.append(job)
        elif is_prelim_rejected(job.url):
            logger.info(f"FILTERED: Job already rejected by closed/clearance (cached) for {job.company} - {job.title}")
            clearance_stats["jobs_filtered_by_clearance"] += 1
            clearance_stats["clearance_filtered_jobs"].append(job)
        else:
            jobs_to_check.append(job)

    limit = int(os.environ.get("PROCESS_LIMIT", "0") or "0")
    if limit > 0 and len(jobs_to_check) > limit:
        logger.info(f"PROCESS_LIMIT={limit}: capping {len(jobs_to_check)} unseen jobs to {limit} for this run")
        jobs_to_check = jobs_to_check[:limit]

    if jobs_to_check:
        logger.info(f"Processing {len(jobs_to_check)} jobs for clearance check")

        if batch_enabled():
            processed_jobs = await _process_jobs_batched(jobs_to_check)
        else:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

            async def process_job_with_semaphore(job: JobPosting) -> JobPosting:
                async with semaphore:
                    await asyncio.to_thread(wait_for_network)
                    return await preprocess_job_posting(job)

            logger.info(f"Processing {len(jobs_to_check)} jobs concurrently (max {MAX_CONCURRENT_JOBS} at a time)")
            processed_jobs = await asyncio.gather(
                *[process_job_with_semaphore(job) for job in jobs_to_check]
            )

        for job, final_job in zip(jobs_to_check, processed_jobs):
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


async def async_main(
    retry_failed: bool = False,
    reevaluate_custom: bool = False,
    apply_filter: Optional[str] = None,
) -> RunSummary | None:
    if retry_failed:
        logger.info("RETRY MODE: Processing failed jobs from ai_results.db")
        try:
            stats = await retry_all_failed_jobs()
            logger.info(f"Retry summary: {stats['success']}/{stats['total']} jobs succeeded")
            return None
        except Exception as e:
            logger.error(f"Fatal error in retry mode: {e}")
            raise

    sheet_id = os.environ.get("SHEET_ID")
    job_listings_url = os.environ.get("JOB_LISTINGS_URL")

    if not sheet_id or not job_listings_url:
        logger.error("SHEET_ID and JOB_LISTINGS_URL environment variables must be set")
        raise ValueError("Missing required environment variables")

    if reevaluate_custom:
        logger.info("REEVALUATE MODE: Rechecking custom filter jobs and updating sheet")
        try:
            stats = await reevaluate_all_custom_filtered_jobs(sheet_id, job_listings_url)
            logger.info(
                f"Reevaluation summary: {stats['reevaluated']} jobs reevaluated, "
                f"{stats['now_passed']} now pass, {stats['added_to_sheet']} added to sheet"
            )
            return None
        except Exception as e:
            logger.error(f"Fatal error in reevaluate mode: {e}")
            raise

    if apply_filter:
        if not set_active_filter(apply_filter):
            logger.error(
                f"Filter '{apply_filter}' not found. Available: {list_filter_names()}"
            )
            raise ValueError(f"Unknown filter: {apply_filter}")
        logger.info(
            f"APPLY FILTER MODE: running full pipeline with filter '{apply_filter}' "
            "(new jobs processed, cached content + closed/clearance reused)"
        )

    logger.info("System sleep prevention enabled")
    logger.info(f"Using SHEET_ID: {sheet_id}")
    logger.info(f"Using JOB_LISTINGS_URL: {job_listings_url}")

    try:
        await asyncio.to_thread(wait_for_network)
        client: gspread.client.Client = await asyncio.to_thread(authenticate_gspread)
        sheet: Worksheet = client.open_by_key(sheet_id).worksheet(SHEET_NAME)

        await asyncio.to_thread(wait_for_network)
        postings: List[JobPosting] = await asyncio.to_thread(fetch_job_postings, job_listings_url)
        existing_urls: Set[str] = await asyncio.to_thread(get_existing_urls, sheet)
        new_jobs: List[JobPosting] = filter_job_postings(postings, existing_urls)

        clearance_stats: ClearanceStats = await filter_jobs_by_clearance(new_jobs)

        await asyncio.to_thread(wait_for_network)
        jobs_added: int = await asyncio.to_thread(write_to_sheet, sheet, clearance_stats["jobs_passed"])
        clearance_stats["jobs_added_to_sheet"] = jobs_added

        summary = summarize_filters(postings, existing_urls, clearance_stats)
        summary["config_name"] = os.environ.get("CONFIG_NAME", "unknown")
        return summary
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        raise


def main(
    retry_failed: bool = False,
    reevaluate_custom: bool = False,
    apply_filter: Optional[str] = None,
) -> RunSummary | None:
    with wakepy.keep.presenting():
        return asyncio.run(
            async_main(
                retry_failed=retry_failed,
                reevaluate_custom=reevaluate_custom,
                apply_filter=apply_filter,
            )
        )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Job posting tracker and AI filter")
    parser.add_argument("--retry", action="store_true", help="Retry all failed jobs from ai_results.db")
    parser.add_argument("--reevaluate-custom", action="store_true", help="Reevaluate all custom filter jobs and update sheet")
    parser.add_argument("--batch", action="store_true", help="Use the OpenAI Batch API (50%% cheaper, async) for AI checks")
    parser.add_argument("--limit", type=int, default=None, help="Only AI-check the first N unseen jobs (for testing)")
    parser.add_argument("--apply-filter", default=None, help="Apply a named filter from filters.toml to cached jobs and add passing ones to the sheet")
    args = parser.parse_args()

    if args.batch:
        os.environ["BATCH_MODE"] = "1"
    if args.limit is not None:
        os.environ["PROCESS_LIMIT"] = str(args.limit)

    main(
        retry_failed=args.retry,
        reevaluate_custom=args.reevaluate_custom,
        apply_filter=args.apply_filter,
    )
