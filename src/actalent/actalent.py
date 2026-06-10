from __future__ import annotations

import asyncio
import csv
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import colorlog
import dotenv
import logging
import requests
from bs4 import BeautifulSoup

from core import pittcsc_simplify
from core.paths import DATA_DIR
from peak.peak_technical import EXPERIENCE_ORDER, classify_job
from core.pittcsc_simplify import (
    MAX_CONCURRENT_JOBS,
    ExponentialBackoff,
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
logger = logging.getLogger("actalent")

WIDGETS_URL = "https://careers.actalentservices.com/widgets"
JOB_URL_BASE = "https://careers.actalentservices.com/us/en/job"
PAGE_SIZE = 100
REQUEST_TIMEOUT = 20.0
OUTPUT_CSV = str(DATA_DIR / "actalent_results.csv")

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Referer": "https://careers.actalentservices.com/us/en/search-results",
    "Origin": "https://careers.actalentservices.com",
}


@dataclass(frozen=True)
class ActalentJob:
    seq: str
    title: str
    category: str
    sub_category: str
    type: str
    location: str
    teaser: str
    url: str
    apply_url: str
    skills: Tuple[str, ...] = field(default_factory=tuple)


def _search_page(frm: int) -> Dict:
    payload = {
        "lang": "en_us",
        "deviceType": "desktop",
        "country": "us",
        "pageName": "search-results",
        "ddoKey": "refineSearch",
        "from": frm,
        "jobs": True,
        "counts": True,
        "all_fields": ["category"],
        "size": PAGE_SIZE,
        "siteType": "external",
        "keywords": "",
        "global": True,
        "selected_fields": {},
    }
    response = requests.post(WIDGETS_URL, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()["refineSearch"]


def fetch_all_jobs() -> List[ActalentJob]:
    jobs: Dict[str, ActalentJob] = {}
    backoff = ExponentialBackoff()
    frm = 0
    total = 0

    while True:
        try:
            result = _search_page(frm)
        except (requests.RequestException, KeyError, ValueError) as exc:
            logger.warning(f"Search at from={frm} failed: {exc}, retrying...")
            backoff.wait()
            continue
        backoff.reset()

        total = result.get("totalHits", 0)
        entries = result.get("data", {}).get("jobs", [])
        if not entries:
            break

        for entry in entries:
            seq = entry.get("jobSeqNo") or entry.get("reqId")
            if not seq or seq in jobs:
                continue
            skills = entry.get("ml_skills") or []
            jobs[seq] = ActalentJob(
                seq=seq,
                title=(entry.get("title") or "").strip(),
                category=(entry.get("category") or "").strip(),
                sub_category=(entry.get("subCategory") or "").strip(),
                type=(entry.get("type") or "").strip(),
                location=(entry.get("cityStateCountry") or entry.get("location") or "").strip(),
                teaser=(entry.get("descriptionTeaser") or "").strip(),
                url=f"{JOB_URL_BASE}/{seq}",
                apply_url=entry.get("applyUrl", ""),
                skills=tuple(skills[:15]),
            )

        logger.info(f"Fetched from={frm}: {len(jobs)}/{total} jobs collected")
        frm += PAGE_SIZE
        if frm >= total:
            break

    logger.info(f"Total unique jobs fetched: {len(jobs)}")
    return list(jobs.values())


def fetch_job_description(seq: str) -> str:
    payload = {
        "lang": "en_us",
        "deviceType": "desktop",
        "country": "us",
        "ddoKey": "jobDetail",
        "pageName": "job-details",
        "jobSeqNo": seq,
        "siteType": "external",
    }
    try:
        response = requests.post(WIDGETS_URL, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        html = response.json()["jobDetail"]["data"]["job"].get("description", "") or ""
    except (requests.RequestException, KeyError, ValueError) as exc:
        logger.warning(f"Failed to fetch description for {seq}: {exc}")
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()


def build_content(job: ActalentJob, description: str) -> str:
    skills = ", ".join(job.skills)
    return (
        f"Title: {job.title}\n"
        f"Category: {job.category} / {job.sub_category}\n"
        f"Type: {job.type}\n"
        f"Location: {job.location}\n"
        f"Skills: {skills}\n\n"
        f"{description or job.teaser}"
    )


async def process_job(job: ActalentJob) -> Optional[Tuple[ActalentJob, str]]:
    cached = get_ai_result(job.url)
    if cached and cached.get("check_type") == "classify" and cached.get("status") in {"passed", "rejected"}:
        if cached["status"] == "rejected":
            logger.info(f"NON-SOFTWARE (cached): {job.title}")
            return None
        experience = cached.get("reason") or "Not Specified"
        logger.info(f"SOFTWARE (cached): {job.title} [{experience}]")
        return job, experience

    description = await asyncio.to_thread(fetch_job_description, job.seq)
    experience = await classify_job(job, build_content(job, description))
    if experience is None:
        logger.info(f"NON-SOFTWARE: {job.title}")
        return None
    logger.info(f"SOFTWARE: {job.title} [{experience}]")
    return job, experience


CSV_HEADER = ["experience_level", "title", "category", "type", "location", "url", "apply_url"]


def csv_row(job: ActalentJob, experience: str) -> List[str]:
    return [experience, job.title, job.sub_category or job.category, job.type, job.location, job.url, job.apply_url]


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
    write_lock = asyncio.Lock()
    counts: Dict[str, int] = {level: 0 for level in EXPERIENCE_ORDER}
    kept = 0

    f = open(OUTPUT_CSV, "w", newline="")
    writer = csv.writer(f)
    writer.writerow(CSV_HEADER)
    f.flush()

    async def with_semaphore(job: ActalentJob) -> None:
        nonlocal kept
        async with semaphore:
            await asyncio.to_thread(wait_for_network)
            result = await process_job(job)
        if result is None:
            return
        job_obj, experience = result
        async with write_lock:
            writer.writerow(csv_row(job_obj, experience))
            f.flush()
            counts[experience] = counts.get(experience, 0) + 1
            kept += 1

    logger.info(f"Classifying {len(jobs)} jobs (max {MAX_CONCURRENT_JOBS} concurrent)")
    try:
        await asyncio.gather(*[with_semaphore(job) for job in jobs])
    finally:
        f.close()

    logger.info(f"Wrote {kept} software jobs to {OUTPUT_CSV}")

    logger.info("=" * 60)
    logger.info("ACTALENT CLASSIFICATION SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total fetched: {len(jobs)}")
    logger.info(f"Software (kept): {kept}")
    logger.info(f"Non-software (filtered): {len(jobs) - kept}")
    for level in EXPERIENCE_ORDER:
        logger.info(f"  ├─ {level}: {counts[level]}")
    logger.info("=" * 60)
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
