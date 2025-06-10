from __future__ import annotations
import logging
import sys
import os
from dataclasses import dataclass
from typing import List, Set
import datetime

import dotenv
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from gspread.utils import ValueInputOption
from gspread.worksheet import Worksheet

# Load environment variables
dotenv.load_dotenv()

# Constants
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
SHEET_ID: str = os.environ["SHEET_ID"]  # Raises if missing
SHEET_NAME: str = "Job Application Tracker"
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
FOUND_SOURCE_DEFAULT: str = "Direct Application"
JOB_LISTINGS_URL: str = os.environ["JOB_LISTINGS_URL"]
FALLBACK_CUTOFF_DATE: str = "2025-03-01"
FALLBACK_CUTOFF_TS: int = int(
    datetime.datetime.fromisoformat(FALLBACK_CUTOFF_DATE).timestamp()
)

# Configure root logger to stdout only
logging.basicConfig(
    level=logging.DEBUG, format=LOG_FORMAT, handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("job_tracker")


@dataclass(frozen=True)
class JobPosting:
    company: str
    locations: List[str]
    title: str
    url: str
    terms: List[str]
    active: bool
    date_posted: int


def authenticate_gspread() -> gspread.client.Client:
    """
    Authenticate with Google Sheets API via service account.
    """
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


def fetch_job_postings(url: str, timeout: float = 10.0) -> List[JobPosting]:
    """
    Fetch and parse job postings from JSON endpoint.
    """
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.error(f"Failed to fetch postings: {exc}")
        return []

    postings: List[JobPosting] = []
    for entry in data:
        # pre process terms
        terms = []
        if "terms" in entry:
            terms = entry["terms"]
        elif "seasons" in entry:
            # Fallback for older entries that use "seasons"
            terms = entry["seasons"]

        postings.append(
            JobPosting(
                company=entry.get("company_name", ""),
                locations=entry.get("locations", []),
                title=entry.get("title", ""),
                url=entry.get("url", "")
                .replace("?utm_source=Simplify&ref=Simplify", "")
                .replace("&utm_source=Simplify&ref=Simplify", ""),
                terms=terms,
                active=bool(entry.get("active", False)),
                date_posted=int(entry.get("date_posted", 0)),
            )
        )
    logger.info(f"Fetched {len(postings)} job postings.")
    return postings


def is_location_excluded(location: str) -> bool:
    return any(loc in location.lower() for loc in EXCLUDED_LOCATIONS)


def is_terms_included(terms: List[str]) -> bool:
    return any(term in INCLUDED_TERMS for term in terms)


def filter_job_postings(
    postings: List[JobPosting], existing_urls: Set[str]
) -> List[JobPosting]:
    """
    Filter postings by activity, location, terms (or fallback), and duplicates.
    """
    filtered: List[JobPosting] = []
    for job in postings:
        if not job.active:
            logger.debug(f"Skipping inactive: {job.company}")
            continue
        if is_location_excluded(" ".join(job.locations)):
            logger.debug(f"Skipping location: {job.locations}")
            continue
        # Terms filtering, with fallback for missing terms
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
        if job.url in existing_urls:
            logger.debug(f"Skipping duplicate URL: {job.url}")
            continue
        filtered.append(job)
    logger.info(f"{len(filtered)} new postings after filtering.")
    return filtered


def get_existing_urls(sheet: Worksheet) -> Set[str]:
    rows = sheet.get_all_values()
    return {row[5] for row in rows if len(row) > 5}


def write_to_sheet(sheet: Worksheet, jobs: List[JobPosting]) -> None:
    """
    Append new job postings to the Google Sheet.
    """
    if not jobs:
        logger.info("No new jobs to add.")
        return

    existing_rows = sheet.get_all_values()
    start_row = len(existing_rows) + 1
    rows_to_add: List[List[str]] = []

    for job in jobs:
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
        logger.info(f"Added {len(rows_to_add)} new rows.")
    except Exception as exc:
        logger.error(f"Failed to update sheet: {exc}")


def summarize_filters(
    postings: List[JobPosting], existing_urls: Set[str]
) -> None:
    excluded_locations: Set[str] = set()
    excluded_terms: Set[str] = set()
    summary = {
        "excluded_locations": excluded_locations,
        "excluded_terms": excluded_terms,
        "location_excluded_jobs": [],
        "term_excluded_jobs": [],
        "date_excluded_jobs": [],
        "duplicate_jobs": [],
        "passed_jobs": [],
        "inactive_jobs": [],
    }

    for job in postings:
        if not job.active:
            summary["inactive_jobs"].append(job)
            continue

        if is_location_excluded(" ".join(job.locations)):
            summary["location_excluded_jobs"].append(job)
            for loc in job.locations:
                if is_location_excluded(loc):
                    excluded_locations.add(loc)
            continue

        if job.terms:
            if not is_terms_included(job.terms):
                summary["term_excluded_jobs"].append(job)
                for term in job.terms:
                    if term not in INCLUDED_TERMS:
                        excluded_terms.add(term)
                continue
        else:
            if job.date_posted < FALLBACK_CUTOFF_TS:
                summary["date_excluded_jobs"].append(job)
                continue

        if job.url in existing_urls:
            summary["duplicate_jobs"].append(job)
            continue

        summary["passed_jobs"].append(job)

    logger.info("Filter Summary:")
    logger.info(f"Excluded Locations: {summary['excluded_locations']}")
    logger.info(f"Excluded Terms: {summary['excluded_terms']}")
    logger.info(f"Location Excluded Jobs: {len(summary['location_excluded_jobs'])}")
    logger.info(f"Term Excluded Jobs: {len(summary['term_excluded_jobs'])}")
    logger.info(f"Date Excluded Jobs: {len(summary['date_excluded_jobs'])}")
    logger.info(f"Duplicate Jobs: {len(summary['duplicate_jobs'])}")
    logger.info(f"Passed Jobs: {len(summary['passed_jobs'])}")
    logger.info(f"Inactive Jobs: {len(summary['inactive_jobs'])}")


def main() -> None:
    client = authenticate_gspread()
    sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
    postings = fetch_job_postings(JOB_LISTINGS_URL)
    existing_urls = get_existing_urls(sheet)
    new_jobs = filter_job_postings(postings, existing_urls)
    summarize_filters(postings, existing_urls)


    write_to_sheet(sheet, new_jobs)


if __name__ == "__main__":
    main()
