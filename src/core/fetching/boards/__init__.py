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
from urllib.parse import urlparse

import requests

from core.fetching.boards.amazon import _amazon
from core.fetching.boards.apple import _APPLE_PAGE, _apple
from core.fetching.boards.ashby import _ashby
from core.fetching.boards.avature import _avature
from core.fetching.boards.bytedance import _BYTEDANCE_HOSTS, _BYTEDANCE_SEARCH, _bytedance
from core.fetching.boards.common import PartialPull, _session
from core.fetching.boards.common import posting_from_listing as _posting
from core.fetching.boards.dates import posted_ts
from core.fetching.boards.eightfold import _eightfold
from core.fetching.boards.goldman import _goldman
from core.fetching.boards.greenhouse import _greenhouse
from core.fetching.boards.ibm import _ibm
from core.fetching.boards.icims import _icims
from core.fetching.boards.jibe import _jibe
from core.fetching.boards.lever import _lever
from core.fetching.boards.markdown import _markdown, parse_markdown
from core.fetching.boards.oracle import _oracle
from core.fetching.boards.smartrecruiters import _smartrecruiters
from core.fetching.boards.successfactors import _successfactors
from core.fetching.boards.taleo import _TALEO_SECTION, _taleo
from core.fetching.boards.workable import _workable
from core.fetching.boards.workday import (
    _WORKDAY_PAGE,
    _WORKDAY_WINDOW,
    _workday,
    _workday_locations,
)
from core.fetching.hosts import EIGHTFOLD_PCSX, EIGHTFOLD_V2
from core.fetching.listings import fetch_job_postings
from core.fetching.posting import JobPosting

__all__ = [
    "AUTHORITATIVE",
    "NEEDS_COMPANY",
    "_APPLE_PAGE",
    "_WORKDAY_PAGE",
    "_WORKDAY_WINDOW",
    "PartialPull",
    "_posting",
    "_session",
    "_workday_locations",
    "fetch_listings",
    "kind",
    "parse_markdown",
    "posted_ts",
    "requests",
]

logger = logging.getLogger(__name__)


def kind(url: str) -> str:
    """Which fetcher a listings URL selects. Also what the admin route uses to
    decide whether the source needs a company name."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "boards-api.greenhouse.io":
        return "greenhouse"
    if host in ("api.lever.co", "api.eu.lever.co"):
        return "lever"
    if host == "api.ashbyhq.com":
        return "ashby"
    if host.endswith("myworkdayjobs.com") and "/wday/cxs/" in parsed.path:
        return "workday"
    if host == "api.smartrecruiters.com":
        return "smartrecruiters"
    if host.endswith("oraclecloud.com") and "/hcmRestApi/" in parsed.path:
        return "oracle"
    if host == "apply.workable.com" and parsed.path.startswith("/api/"):
        return "workable"
    if host.endswith(".avature.net"):
        return "avature"
    if host.endswith(".taleo.net") and _TALEO_SECTION.match(parsed.path):
        return "taleo"
    if host == "jobs.apple.com" and parsed.path.startswith("/api/"):
        return "apple"
    if host in _BYTEDANCE_HOSTS and parsed.path == _BYTEDANCE_SEARCH:
        return "bytedance"
    # An iCIMS portal is a subdomain per tenant, not always careers-<name>
    # (expleo-jobs-us-en.icims.com); the search page is the listing.
    if host.endswith(".icims.com") and parsed.path.rstrip("/") == "/jobs/search":
        return "icims"
    # iCIMS's hosted career sites (Jibe) sit on the employer's own domain and
    # answer one path: careers.amd.com/api/jobs, careers.spiritaero.com/api/jobs.
    if parsed.path.rstrip("/") == "/api/jobs":
        return "jibe"
    if host == "www-api.ibm.com" and parsed.path == "/search/api/v2":
        return "ibm"
    if host == "api-higher.gs.com" and parsed.path == "/gateway/api/v1/graphql":
        return "goldman"
    if host == "www.amazon.jobs" and parsed.path.endswith("/search.json"):
        return "amazon"
    # SuccessFactors Career Site Builder runs on each employer's own domain
    # (jobs.l3harris.com, jobs.ulalaunch.com), so the feed path is the marker.
    if parsed.path == "/services/rss/job/":
        return "successfactors"
    # Eightfold tenants serve from their own domains (jobs.northropgrumman.com,
    # apply.careers.microsoft.com), so the path is the only mark of the format.
    if parsed.path.rstrip("/") in (EIGHTFOLD_PCSX, EIGHTFOLD_V2):
        return "eightfold"
    if parsed.path.endswith(".md"):
        return "markdown"
    return "sheet_era"


# Lever, Ashby, Workday, Oracle, Workable, Taleo, Apple, ByteDance, the iCIMS
# portal and SuccessFactors list a company's own openings and never say whose
# (the iCIMS portal names it only in its page title; a SuccessFactors feed's
# channel title is whatever the site was named, "L3HHCM20 - Custom Search" on
# L3Harris); Greenhouse, SmartRecruiters and Jibe (hiring_organization) name
# the company on every job and the aggregators name it per row. Goldman's
# roles carry no company, and IBM's carry the hiring legal entity ("(0063) IBM
# India Private Limited") rather than the name a person searches for. Amazon
# names a legal entity per row too ("Amazon.com Services LLC", "ADCI HYD 13
# SEZ"). An Eightfold row names a department and never the company, and an
# Avature search page never names it (Koch's names a brand, "Molex").
NEEDS_COMPANY = frozenset(
    {
        "avature",
        "lever",
        "ashby",
        "workday",
        "oracle",
        "workable",
        "taleo",
        "apple",
        "bytedance",
        "icims",
        "ibm",
        "goldman",
        "amazon",
        "successfactors",
        "eightfold",
    }
)

# A company's own board lists every open posting, so a posting missing from
# it is closed. An aggregator list trims old rows on its own schedule, so
# absence there says nothing; those postings close through the reverify
# sweep instead. A Taleo careersection is a company's own board, but its
# search counts rows it never lists, and a pull short of that count raises
# PartialPull and retires nothing (see _taleo).
# The iCIMS portal, Jibe, Eightfold and Avature are a company's own board, and
# each fetcher raises PartialPull when it cannot show it read all of it.
AUTHORITATIVE = frozenset(
    {
        "avature",
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "smartrecruiters",
        "oracle",
        "workable",
        "taleo",
        "apple",
        "bytedance",
        "icims",
        "jibe",
        "ibm",
        "goldman",
        "amazon",
        "successfactors",
        "eightfold",
    }
)


def fetch_listings(url: str, company: str | None = None) -> list[JobPosting]:
    fetcher = {
        "greenhouse": _greenhouse,
        "lever": _lever,
        "ashby": _ashby,
        "workday": _workday,
        "smartrecruiters": _smartrecruiters,
        "oracle": _oracle,
        "workable": _workable,
        "taleo": _taleo,
        "apple": _apple,
        "bytedance": _bytedance,
        "icims": _icims,
        "jibe": _jibe,
        "ibm": _ibm,
        "goldman": _goldman,
        "amazon": _amazon,
        "successfactors": _successfactors,
        "eightfold": _eightfold,
        "avature": _avature,
        "markdown": _markdown,
    }.get(kind(url))
    if fetcher is None:
        return fetch_job_postings(url)
    postings = fetcher(url, company or "")
    logger.info(f"Fetched {len(postings)} postings from {url}")
    return postings
