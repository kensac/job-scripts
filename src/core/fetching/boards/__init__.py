"""Listing fetchers: one per board format, chosen by the listings URL.

A source is a URL, so the format is read off the URL rather than stored beside
it. The applicant-tracking systems publish their boards on hosts of their own,
and the GitHub aggregators publish a markdown table or a JSON file. Every
fetcher returns the same JobPosting, so ingest, the catalog and the checks
never learn which board a posting came from.

A company board lists every opening, most of them senior. Ingest normally
applies the source's title_pattern before anything downstream sees the
postings, because verify_new checks every active posting with cached text
regardless of who subscribed: SpaceX listed 2,309 openings on 2026-09-04, of
which 68 read as entry level, and without the pattern the other 2,241 would
each cost a closed and a clearance check. The persisted fleet switch can bypass
admission without deleting the pattern or its match evidence.
"""

from __future__ import annotations

import logging

import requests

from core.fetching.boards.apple import _APPLE_PAGE
from core.fetching.boards.common import PartialPull, _session
from core.fetching.boards.common import posting_from_listing as _posting
from core.fetching.boards.dates import posted_ts
from core.fetching.boards.formats import FORMATS, BoardFormat, format_for_url
from core.fetching.boards.markdown import parse_markdown
from core.fetching.boards.workday import _WORKDAY_PAGE, _WORKDAY_WINDOW, _workday_locations
from core.fetching.listings import fetch_job_postings
from core.fetching.posting import JobPosting

__all__ = [
    "AUTHORITATIVE",
    "NEEDS_COMPANY",
    "_APPLE_PAGE",
    "_WORKDAY_PAGE",
    "_WORKDAY_WINDOW",
    "BoardFormat",
    "PartialPull",
    "_posting",
    "_session",
    "_workday_locations",
    "fetch_listings",
    "format_for_url",
    "kind",
    "parse_markdown",
    "posted_ts",
    "requests",
]

logger = logging.getLogger(__name__)


# Keep the established public sets derived from the format catalog.
NEEDS_COMPANY = frozenset(
    board_format.name for board_format in FORMATS if board_format.needs_company
)
AUTHORITATIVE = frozenset(
    board_format.name for board_format in FORMATS if board_format.authoritative
)


def kind(url: str) -> str:
    """The listing format selected by a source URL."""
    return format_for_url(url).name


def fetch_listings(url: str, company: str | None = None) -> list[JobPosting]:
    board_format = format_for_url(url)
    if board_format.fetcher is None:
        return fetch_job_postings(url)
    postings = board_format.fetcher(url, company or "")
    logger.info(f"Fetched {len(postings)} postings from {url}")
    return postings
