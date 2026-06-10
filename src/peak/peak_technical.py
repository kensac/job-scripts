from __future__ import annotations

import asyncio
import csv
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

import colorlog
import dotenv
import logging
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from core import pittcsc_simplify
from core.paths import DATA_DIR
from core.pittcsc_simplify import (
    MAX_CONCURRENT_JOBS,
    OPENAI_MAX_COMPLETION_TOKENS,
    OPENAI_MAX_RETRIES,
    OPENAI_TIMEOUT,
    ExponentialBackoff,
    add_ai_result,
    get_ai_result,
    wait_for_network,
)

dotenv.load_dotenv()

handler = colorlog.StreamHandler(sys.stdout)
handler.setFormatter(
    colorlog.ColoredFormatter(
        "%(asctime)s - %(log_color)s%(levelname)s%(reset)s - %(message)s",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )
)
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("peak_technical")

SEARCH_URL = "https://ws.jobdiva.com/candPortal/rest/job/searchjobsportal"
DETAIL_URL = "https://ws.jobdiva.com/candPortal/rest/job/getdetailbyjobid"
PORTAL_ID = "2320"
PORTAL_TOKEN = os.environ.get("PEAK_TOKEN", "01dFgdTBBNcXFYIAwhaAVASX1RGAV9VWwNXBAMrdHc=")
PORTAL_A = os.environ.get("PEAK_A", "gnjdnw4p7dcsh8maybnicg1a02iav20910i4tq2giw4936vpiu8xi6grvip5n9mz")

PAGE_SIZE = 100
REQUEST_TIMEOUT = 15.0
DESC_CHAR_LIMIT = 4000
OUTPUT_CSV = str(DATA_DIR / "peak_results.csv")

ExperienceLevel = Literal[
    "Entry Level / New Grad",
    "Mid (2-4 years)",
    "Senior (5-7 years)",
    "Staff/Principal (8+ years)",
    "Not Specified",
]
EXPERIENCE_ORDER: List[str] = [
    "Entry Level / New Grad",
    "Mid (2-4 years)",
    "Senior (5-7 years)",
    "Staff/Principal (8+ years)",
    "Not Specified",
]

HEADERS = {
    "Accept": "*/*",
    "Origin": "https://jobs.peaktechnical.com",
    "Referer": "https://jobs.peaktechnical.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "a": PORTAL_A,
    "content-type": "application/x-www-form-urlencoded",
    "portalid": PORTAL_ID,
    "token": PORTAL_TOKEN,
}

CLASSIFY_SYSTEM_PROMPT = """You classify job postings. Return two fields.

1. is_software: true if the role is a software/technology engineering role or closely adjacent: software engineer, backend/frontend/full-stack, data engineer/scientist, ML/AI, DevOps/SRE/platform/cloud/infrastructure, security engineer, QA/test automation, mobile, embedded/firmware, systems engineer (software), or a technical role whose core work is writing/maintaining code. false for non-software roles: sales, marketing, recruiting, admin, finance, healthcare, skilled trades, manufacturing, and non-software engineering (mechanical, electrical, civil, chemical, etc.).

2. experience_level: years of professional experience required, exactly one of:
- "Entry Level / New Grad": 0-1 years, internships, new grad, or junior
- "Mid (2-4 years)"
- "Senior (5-7 years)"
- "Staff/Principal (8+ years)"
- "Not Specified": requirement unclear and title gives no signal

Base experience_level on stated requirements; if none stated, infer from title seniority; otherwise "Not Specified". Be decisive and concise."""


@dataclass(frozen=True)
class PeakJob:
    id: int
    title: str
    company: str
    location: str
    pay_rate: str
    url: str
    description: str = ""


class JobClassification(BaseModel):
    is_software: bool = Field(description="Whether the role is software/technology or closely adjacent")
    experience_level: ExperienceLevel = Field(description="Required experience bucket")


def _search_page(frm: int, to: int) -> Dict:
    body = (
        f"city=&country=&from={frm}&jobCategories=&jobDivisions=&jobTypes="
        f"&keywords=&miles=&onsiteFlex=&portalID=1&qualifications=&states="
        f"&to={to}&unit=mi&zipcode="
    )
    response = requests.post(SEARCH_URL, headers=HEADERS, data=body, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _clean_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def fetch_all_jobs() -> List[PeakJob]:
    jobs: Dict[int, PeakJob] = {}
    backoff = ExponentialBackoff()
    page = 0
    total = 0

    while True:
        frm = page * PAGE_SIZE + 1
        to = (page + 1) * PAGE_SIZE
        try:
            data = _search_page(frm, to)
        except requests.RequestException as exc:
            logger.warning(f"Search page {page} failed: {exc}, retrying...")
            backoff.wait()
            continue
        backoff.reset()

        total = data.get("total", 0)
        entries = data.get("data", [])
        new_count = 0
        for entry in entries:
            job_id = entry["id"]
            if job_id in jobs:
                continue
            jobs[job_id] = PeakJob(
                id=job_id,
                title=(entry.get("title") or "").strip(),
                company=(entry.get("company") or "").strip(),
                location=(entry.get("location") or "").strip(),
                pay_rate=(entry.get("payRate") or "").strip(),
                url=f"{DETAIL_URL}/{job_id}",
                description=_clean_html(entry.get("jobDescription", "")),
            )
            new_count += 1

        logger.info(f"Fetched page {page}: {len(jobs)}/{total} jobs collected")

        if not entries or new_count == 0 or len(jobs) >= total:
            break
        page += 1

    logger.info(f"Total unique jobs fetched: {len(jobs)}")
    return list(jobs.values())


def fetch_job_description(job_id: int) -> str:
    try:
        response = requests.get(
            f"{DETAIL_URL}/{job_id}", headers=HEADERS, params={"compid": ""}, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return _clean_html(response.json().get("job", {}).get("jobDescription", ""))
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"Failed to fetch detail for job {job_id}: {exc}")
        return ""


def build_content(job: PeakJob, description: str) -> str:
    body = (description or job.description)[:DESC_CHAR_LIMIT]
    return f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n\n{body}"


async def classify_job(job: PeakJob, content: str) -> Optional[str]:
    client = pittcsc_simplify.openai_client
    if not client:
        return None

    backoff = ExponentialBackoff(base_delay=2.0, max_delay=30.0)
    for attempt in range(OPENAI_MAX_RETRIES):
        try:
            response = await client.beta.chat.completions.parse(
                model="gpt-5-nano",
                messages=[
                    {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Classify this job:\n\n{content}"},
                ],
                response_format=JobClassification,
                max_completion_tokens=OPENAI_MAX_COMPLETION_TOKENS,
                timeout=OPENAI_TIMEOUT,
            )

            parsed = response.choices[0].message.parsed
            if not parsed:
                logger.warning(f"No parsed classification for {job.title}")
                return None

            usage = response.usage
            status = "passed" if parsed.is_software else "rejected"
            add_ai_result(
                job.url, status, parsed.experience_level, "classify",
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
            )
            return parsed.experience_level if parsed.is_software else None

        except Exception as exc:
            exc_str = str(exc).lower()
            if any(k in exc_str for k in ("timeout", "connection", "network")):
                logger.warning(f"Network error during classify: {exc}")
                await asyncio.to_thread(wait_for_network)
                continue
            if attempt < OPENAI_MAX_RETRIES - 1:
                logger.warning(f"Classify failed (attempt {attempt + 1}/{OPENAI_MAX_RETRIES}): {exc}")
                await asyncio.to_thread(backoff.wait)
            else:
                logger.warning(f"Classify failed after {OPENAI_MAX_RETRIES} attempts: {exc}")
                add_ai_result(job.url, "failed", f"classify failed: {str(exc)[:100]}", "classify")
                return None
    return None


async def process_job(job: PeakJob) -> Optional[Tuple[PeakJob, str]]:
    cached = get_ai_result(job.url)
    if cached and cached.get("check_type") == "classify" and cached.get("status") in {"passed", "rejected"}:
        if cached["status"] == "rejected":
            logger.info(f"NON-SOFTWARE (cached): {job.company or 'N/A'} - {job.title}")
            return None
        experience = cached.get("reason") or "Not Specified"
        logger.info(f"SOFTWARE (cached): {job.company or 'N/A'} - {job.title} [{experience}]")
        return job, experience

    description = await asyncio.to_thread(fetch_job_description, job.id)
    content = build_content(job, description)
    experience = await classify_job(job, content)
    if experience is None:
        logger.info(f"NON-SOFTWARE: {job.company or 'N/A'} - {job.title}")
        return None
    logger.info(f"SOFTWARE: {job.company or 'N/A'} - {job.title} [{experience}]")
    return job, experience


def write_csv(results: List[Tuple[PeakJob, str]]) -> None:
    order = {level: i for i, level in enumerate(EXPERIENCE_ORDER)}
    results.sort(key=lambda r: (order.get(r[1], len(order)), r[0].company.lower()))
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["experience_level", "title", "company", "location", "pay_rate", "url"])
        for job, experience in results:
            writer.writerow([experience, job.title, job.company, job.location, job.pay_rate, job.url])
    logger.info(f"Wrote {len(results)} software jobs to {OUTPUT_CSV}")


async def async_main() -> int:
    if not pittcsc_simplify.openai_client:
        logger.error("OPENAI_API_KEY not set - cannot run AI classification")
        return 1

    await asyncio.to_thread(wait_for_network)
    jobs = await asyncio.to_thread(fetch_all_jobs)
    if not jobs:
        logger.error("No jobs fetched")
        return 1

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

    async def with_semaphore(job: PeakJob) -> Optional[Tuple[PeakJob, str]]:
        async with semaphore:
            await asyncio.to_thread(wait_for_network)
            return await process_job(job)

    logger.info(f"Classifying {len(jobs)} jobs (max {MAX_CONCURRENT_JOBS} concurrent)")
    results = [r for r in await asyncio.gather(*[with_semaphore(job) for job in jobs]) if r is not None]

    write_csv(results)

    counts: Dict[str, int] = {level: 0 for level in EXPERIENCE_ORDER}
    for _, experience in results:
        counts[experience] = counts.get(experience, 0) + 1

    logger.info("=" * 60)
    logger.info("PEAK TECHNICAL CLASSIFICATION SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total fetched: {len(jobs)}")
    logger.info(f"Software (kept): {len(results)}")
    logger.info(f"Non-software (filtered): {len(jobs) - len(results)}")
    for level in EXPERIENCE_ORDER:
        logger.info(f"  ├─ {level}: {counts[level]}")
    logger.info("=" * 60)
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
