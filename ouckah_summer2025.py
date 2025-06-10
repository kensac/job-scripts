from logging.handlers import RotatingFileHandler
import os
import logging
import re
from typing import List, Dict, Any
import dotenv
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials  # type: ignore
import warnings

dotenv.load_dotenv()

# Constants
LOG_FILE = "./pittcsc_simplify.log"
MAX_FILE_SIZE = 128 * 1024 * 1024  # Max size in bytes (128 MB)
BACKUP_COUNT = 5  # Number of backup files to keep

# Formatter
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# File handler setup with rotation
file_handler = RotatingFileHandler(
    LOG_FILE,
    mode="a",
    maxBytes=MAX_FILE_SIZE,
    backupCount=BACKUP_COUNT,
    encoding=None,
    delay=0,
)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

# Console handler setup
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.INFO)

# Logger setup
logger = logging.getLogger("")
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Constants
SHEET_ID = os.getenv("SHEET_ID")
SHEET_NAME = "Job Application Tracker"
FOUND_SOURCE_DEFAULT = "Direct Application"
COLUMNS = [
    "blank",
    "company",
    "size",
    "location",
    "foundSource",
    "appLink",
    "role",
    "requirements",
    "recruiter",
    "connection1",
    "connection2",
    "documents",
    "dateApplied",
    "status",
    "comments",
]
EXCLUDED_LOCATIONS = [
    "canada",
    "toronto",
    "montreal",
    "ontario",
    "london",
    "--------",
]


def authenticate_gspread() -> gspread.Client:  # type: ignore
    """
    Authenticate with the Google Sheets API using OAuth2 credentials.

    Returns:
        gspread.Client: Authenticated Google Sheets client.
    """
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(  # type: ignore
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS_CUSTOM"), scope
    )  # type: ignore
    client = gspread.authorize(creds)  # type: ignore
    logging.info("Google Spreadsheet authenticated.")
    return client


def fetch_job_postings(url: str) -> List[List[str]]:
    """
    Fetch job postings from a given URL containing markdown formatted table data.

    Args:
        url (str): URL to fetch job postings from.

    Returns:
        List[List[str]]: List of job postings represented as lists of strings.
    """
    response = requests.get(url, timeout=10)
    lines = response.text.split("\n")
    job_postings: List[List[str]] = []
    read_table = False

    for line in lines:
        if "TABLE_START" in line:
            read_table = True
        elif "TABLE_END" in line:
            read_table = False
        elif read_table and line.startswith("|") and line.endswith("|"):
            columns = [col.strip() for col in line.strip("|").split("|")]
            if len(columns) > 1 and "🔒" not in columns:
                job_postings.append(columns)
    logging.info(f"Fetched job postings from {url}")
    return job_postings


def parse_job_data(job: List[str], config: Dict[str, Any]) -> Dict[str, str]:
    """
    Parse individual job data into a structured dictionary based on configuration mappings.

    Args:
        job (List[str]): List of job data strings from a table row.
        config (Dict[str, Any]): Configuration for column mappings and regex patterns.

    Returns:
        Dict[str, str]: Structured job information.
    """
    job_info: dict[Any, Any] = {}
    for key, index in config["columnMapping"]["readColumns"].items():
        data: Any = job[index]
        regex = config["columnMapping"]["regex"].get(key, None)
        if regex:
            match: Any = re.search(regex, data)
            job_info[key] = match.group(1) if match else data
        else:
            job_info[key] = data
    logging.info(f"Parsed job data for {job_info.get('company')}")
    return job_info


def is_excluded_location(location: str) -> bool:
    """
    Determine if a location is in the list of excluded locations.

    Args:
        location (str): The location string to check.

    Returns:
        bool: True if the location is excluded, otherwise False.
    """
    excluded = any(
        excluded.lower() in location.lower() for excluded in EXCLUDED_LOCATIONS
    )
    if excluded:
        logging.info(f"Excluded location found: {location}")
    return excluded


def passes_keyword_filter(
    job_info: Dict[str, str], keyword_filter: Dict[str, Any]
) -> bool:
    """
    Check if the job information passes the keyword filter.

    Args:
        job_info (Dict[str, str]): Dictionary containing job data.
        keyword_filter (Dict[str, Any]): Filter settings with enabled status and keywords list.

    Returns:
        bool: True if the job info passes the keyword filter, otherwise False.
    """
    if keyword_filter["enabled"]:
        match = any(
            keyword.lower() in job_info["role"].lower()
            for keyword in keyword_filter["keywords"]
        )
        if not match:
            logging.info(f"Keyword filter failed for role: {job_info['role']}")
        return match
    return True


def update_job_postings(config: Dict[str, Any]) -> None:
    """
    Update the Google Spreadsheet with new job postings that meet the criteria defined in config.

    Args:
        config (Dict[str, Any]): Configuration for fetching and parsing job postings.
    """
    client = authenticate_gspread()
    sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)  # type: ignore
    existing_data = sheet.get_all_values()
    existing_links: list[Any] = [
        row[5] for row in existing_data if len(row) > 5
    ]  # type: ignore

    job_postings = fetch_job_postings(config["githubUrl"])
    next_row = len(existing_data) + 1
    new_rows = []

    for job in job_postings:
        job_info = parse_job_data(job, config)
        new_link = job_info["appLink"] not in existing_links
        valid_location = not is_excluded_location(job_info["location"])
        keyword_match = passes_keyword_filter(job_info, config["keywordFilter"])

        if new_link and valid_location and keyword_match:
            row = ["" for _ in range(15)]
            row[1] = job_info["company"]
            row[3] = job_info["location"]
            row[4] = FOUND_SOURCE_DEFAULT
            row[5] = job_info["appLink"]
            row[6] = job_info["role"]
            new_rows.append(row)  # type: ignore

    if new_rows:
        range_to_update = f"A{next_row}:O{next_row + len(new_rows) - 1}"  # type: ignore
        sheet.update(
            new_rows, range_to_update, value_input_option="USER_ENTERED"  # type: ignore
        )  # type: ignore
        logging.info(
            f"Updated Google Spreadsheet with {len(new_rows)} new rows."
        )  # type: ignore


configs = [
    {
        "githubUrl": "https://raw.githubusercontent.com/Ouckah/Summer2025-Internships/main/README.md",
        "columnMapping": {
            "readColumns": {"company": 0, "location": 2, "appLink": 3, "role": 1},
            "regex": {"company": r"([^|\n]+)", "appLink": r'href="([^"]+)"'},
        },
        "keywordFilter": {
            "enabled": False,
            "keywords": [""],
        },
    },
]
"""
    {
        "githubUrl": "https://raw.githubusercontent.com/SimplifyJobs/Summer2025-Internships/dev/README-Off-Season.md",
        "columnMapping": {
            "readColumns": {"company": 0, "location": 2, "appLink": 4, "role": 1},
            "regex": {"company": r"\[([^\]]+)\]", "appLink": r'href="([^"]+)"'},
        },
        "keywordFilter": {
            "enabled": False,
            "keywords": [""],
        },
    },
    {
        "githubUrl": "https://raw.githubusercontent.com/SimplifyJobs/Summer2025-Internships/dev/README.md",
        "columnMapping": {
            "readColumns": {"company": 0, "location": 2, "appLink": 3, "role": 1},
            "regex": {"company": r"\[([^\]]+)\]", "appLink": r'href="([^"]+)"'},
        },
        "keywordFilter": {"enabled": False, "keywords": [""]},
    },"""


def main() -> None:
    """
    Main function to orchestrate the job posting updates across multiple configurations.
    """

    warnings.warn(
        "This file is deprecated and will be removed in the future. Please use pittcsc_simplify.py instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    for config in configs:  # type: ignore
        update_job_postings(config)  # type: ignore
    logging.info("Job posting update completed.")


if __name__ == "__main__":
    main()
