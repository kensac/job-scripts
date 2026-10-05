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

import datetime
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import ftfy
import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.fetching.ats import (
    ashby_text,
    clean_html,
    goldman_place,
    goldman_text,
    greenhouse_text,
    join,
    lever_text,
)
from core.fetching.listings import fetch_job_postings
from core.fetching.posting import JobPosting
from core.fetching.urls import normalize_url

logger = logging.getLogger(__name__)

TIMEOUT = 30.0

# The hourly cycle is the real retry; this only rides out a blip inside one
# fetch, which the markdown fetcher it replaces also did (three attempts).
_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain"})
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(total=3, backoff_factor=1, status_forcelist=(500, 502, 503, 504))
    ),
)


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
    if parsed.path.rstrip("/") in (_EIGHTFOLD_PCSX, _EIGHTFOLD_V2):
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
# SEZ"). An Eightfold row names a department and never the company.
NEEDS_COMPANY = frozenset(
    {
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
# The iCIMS portal, Jibe and Eightfold are a company's own board, and
# each fetcher raises PartialPull when it cannot show it read all of it.
AUTHORITATIVE = frozenset(
    {
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
        "markdown": _markdown,
    }.get(kind(url))
    if fetcher is None:
        return fetch_job_postings(url)
    postings = fetcher(url, company or "")
    logger.info(f"Fetched {len(postings)} postings from {url}")
    return postings


# The listing fields that are the posting's text, or bulk that duplicates it.
# They go into JobPosting.description (as text) rather than into raw.
_TEXT_FIELDS = frozenset(
    {
        "content",
        "description",
        "descriptionPlain",
        "descriptionHtml",
        "descriptionBody",
        "descriptionBodyPlain",
        "lists",
        "additional",
        "additionalPlain",
        "opening",
        "openingPlain",
    }
)


def _posting(
    company: str,
    title: str | None,
    locations: list,
    url: str | None,
    posted: int,
    raw: dict | None = None,
    description: str = "",
):
    if not title or not url:
        return None
    return JobPosting(
        company=ftfy.fix_text(company).strip(),
        locations=[ftfy.fix_text(str(x)).strip() for x in locations if x and str(x).strip()],
        title=ftfy.fix_text(title).strip(),
        url=normalize_url(url),
        terms=[],
        active=True,
        date_posted=posted,
        raw_url=url,
        description=description.replace("\x00", ""),
        raw=_no_nul({k: v for k, v in (raw or {}).items() if k not in _TEXT_FIELDS}),
    )


def _no_nul(value: Any) -> Any:
    """jsonb refuses \u0000 anywhere in a document; an Oracle requisition
    carried one on 2026-09-05 and every pull of that board failed whole."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: _no_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_nul(v) for v in value]
    return value


def _with_query(url: str, **params: str) -> str:
    """The listings URL with these query parameters added, existing ones kept."""
    parsed = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()} | params
    return urlunparse(parsed._replace(query=urlencode(query)))


def _greenhouse(url: str, company: str) -> list[JobPosting]:
    # content=true returns every posting's text in the one call, so nothing
    # downstream has to fetch the posting to read it.
    resp = _session.get(_with_query(url, content="true"), timeout=TIMEOUT)
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        location = (j.get("location") or {}).get("name") or ""
        p = _posting(
            j.get("company_name") or company,
            j.get("title"),
            location.split(";"),
            j.get("absolute_url"),
            _iso_ts(j.get("first_published")),
            raw=j,
            description=greenhouse_text(j) if j.get("content") else "",
        )
        if p:
            out.append(p)
    return out


def _lever(url: str, company: str) -> list[JobPosting]:
    resp = _session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    out = []
    for j in resp.json():
        cats = j.get("categories") or {}
        p = _posting(
            company,
            j.get("text"),
            cats.get("allLocations") or [cats.get("location")],
            j.get("hostedUrl"),
            int(j.get("createdAt") or 0) // 1000,
            raw=j,
            description=lever_text(j) if j.get("description") or j.get("lists") else "",
        )
        if p:
            out.append(p)
    return out


def _ashby(url: str, company: str) -> list[JobPosting]:
    # The board call already carries every posting's description; asking for
    # compensation too makes it the same text the resolver assembles.
    resp = _session.get(_with_query(url, includeCompensation="true"), timeout=TIMEOUT)
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        if not j.get("isListed", True):
            continue
        p = _posting(
            company,
            j.get("title"),
            [j.get("location"), *[s.get("location") for s in j.get("secondaryLocations") or []]],
            j.get("jobUrl"),
            _iso_ts(j.get("publishedAt")),
            raw=j,
            description=ashby_text(j) if j.get("descriptionHtml") else "",
        )
        if p:
            out.append(p)
    return out


# Workday's job-search endpoint returns at most 20 postings per request
# whatever limit is asked for.
_WORKDAY_PAGE = 20

# Some tenants stop a search at 2,000 results: the first page says total=2000
# and every page past offset 2,000 wraps back to the first. Measured on
# 2026-10-05 on 19 of 355 tenants (Airbus, NVIDIA, Walmart); others page
# straight past it (CVS, 18,841).
_WORKDAY_WINDOW = 2000


class PartialPull(Exception):
    """A pull that could not prove it saw every open posting.

    Ingest admits what it holds and retires nothing by absence, because a
    posting outside what was seen is not evidence of a closure. Closures on
    such a board come from re-verification instead."""

    def __init__(self, postings: list[JobPosting]):
        super().__init__(f"{len(postings)} postings from an incomplete pull")
        self.postings = postings


# A count is not a place. Measured 2026-09-12: 1,002 postings in the catalog
# carry "2 Locations", "3 Locations" and so on as their only location, and 162
# carry none at all. Both defeat a location filter, in opposite directions. A
# count never matches the `locations` vocabulary, so those postings are
# silently EXCLUDED wherever included_locations is set; an empty list is
# deliberately kept by that same predicate, which is how an Accenture posting
# in Jakarta reached a United States board.
_WORKDAY_LOCATION_COUNT = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def _workday_locations(posting: dict) -> list[str]:
    """Where a Workday posting is, from whichever field actually says.

    `locationsText` is the intended field and is right when it names a place.
    Three tenants measured on 2026-09-12 do not send it at all (Accenture,
    Thomson Reuters) or send a count instead (BlackRock, "2 Locations"). The
    place is then only in `externalPath`, which is the posting's own url:
    /job/<Place>/<Title>_<req>. Some postings carry no place segment, and
    those keep returning nothing rather than inventing one.
    """
    text = (posting.get("locationsText") or "").strip()
    if text and not _WORKDAY_LOCATION_COUNT.match(text):
        return [text]
    parts = [segment for segment in (posting.get("externalPath") or "").split("/") if segment]
    if len(parts) >= 3 and parts[0] == "job":
        # Workday slugs a place with hyphens and runs them together for its
        # own separators: "Pernambuco---Recife", "Nova-Lima-Shopping-Alta-Vila".
        # One pass, because replacing "---" first and "-" second eats the
        # hyphen the first replacement just wrote.
        place = re.sub(r"-+", lambda m: " - " if len(m.group()) >= 3 else " ", parts[1]).strip()
        if place:
            return [place]
    return []


def _workday(url: str, company: str) -> list[JobPosting]:
    """POST https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

    A searchText query parameter on the listings URL becomes the search the
    tenant's own careers page would run, which is the only server-side filter
    these boards offer; Boeing returned 334 postings for "new grad" on 2026-09-04.

    A tenant that stops at _WORKDAY_WINDOW is read again in slices, one value
    of its own facets at a time, and the pull is reported partial.
    """
    parsed = urlparse(url)
    search = (parse_qs(parsed.query).get("searchText") or [""])[0]
    endpoint = urlunparse(parsed._replace(query="", fragment=""))
    site = parsed.path.split("/wday/cxs/", 1)[1].split("/")[1]
    base = f"https://{parsed.netloc}/{site}"
    postings, total, facets = _workday_slice(endpoint, base, company, search, {})
    if total != _WORKDAY_WINDOW:
        return postings
    seen = {p.url: p for p in postings}
    for parameter, values in _workday_slicing_facets(facets):
        for value in values:
            part, _, _ = _workday_slice(endpoint, base, company, search, {parameter: [value]})
            seen.update((p.url, p) for p in part)
    raise PartialPull(list(seen.values()))


def _workday_slice(
    endpoint: str, base: str, company: str, search: str, facets: dict[str, list[str]]
) -> tuple[list[JobPosting], int, list[dict]]:
    """Every posting one search returns, its stated total and its facets."""
    out: list[JobPosting] = []
    offset = 0
    # Only the first page carries the count; later pages say total=0. Read
    # per page, that stopped every tenant after two pages: on 2026-10-05, 130
    # of 454 Workday sources held exactly 40 postings (Boeing listed 752), and
    # because the pull is authoritative the rest were retired as closed.
    total: int | None = None
    available: list[dict] = []
    while True:
        resp = _session.post(
            endpoint,
            json={
                "appliedFacets": facets,
                "limit": _WORKDAY_PAGE,
                "offset": offset,
                "searchText": search,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        page = data.get("jobPostings") or []
        for j in page:
            p = _posting(
                company,
                j.get("title"),
                _workday_locations(j),
                base + j.get("externalPath", "") if j.get("externalPath") else None,
                posted_ts(j.get("postedOn") or ""),
            )
            if p:
                out.append(p)
        if total is None:
            total = int(data.get("total") or 0)
            available = data.get("facets") or []
        offset += len(page)
        # Past the window a capped tenant wraps to its first page, so the
        # window bounds the loop even where the total does not.
        if not page or offset >= min(total, _WORKDAY_WINDOW):
            return out, total, available


def _workday_slicing_facets(facets: list[dict]) -> list[tuple[str, list[str]]]:
    """The two facets that cover the most postings with every value under the window.

    Two, because a posting carrying no value of the first is invisible to its
    slices; on Airbus the best single facet covered 2,755 of about 2,940.
    Facets whose largest value reaches the window cannot be read whole.
    """

    def expand(parameter: str, values: list[dict]):
        # A value carrying its own facetParameter and values is a facet in its
        # own right (Airbus's locationMainGroup holds locationCountry), and its
        # ids are only accepted under that inner name: under the outer one the
        # endpoint answers 400.
        leaves = [v for v in values if "values" not in v]
        if leaves:
            yield parameter, leaves
        for group in values:
            if "values" in group and group.get("facetParameter"):
                yield from expand(group["facetParameter"], group["values"])

    usable = []
    for facet in facets:
        for parameter, values in expand(facet["facetParameter"], facet.get("values") or []):
            counts = [int(v.get("count") or 0) for v in values]
            if max(counts) < _WORKDAY_WINDOW:
                usable.append((sum(counts), parameter, [v["id"] for v in values]))
    usable.sort(key=lambda u: u[0], reverse=True)
    return [(parameter, ids) for _, parameter, ids in usable[:2]]


# SmartRecruiters' public postings API pages 100 at a time whatever limit is
# asked for; measured on BoschGroup, 4,813 postings, 2026-09-05.
_SMARTRECRUITERS_PAGE = 100


def _smartrecruiters(url: str, company: str) -> list[JobPosting]:
    """GET https://api.smartrecruiters.com/v1/companies/{id}/postings

    Names the company on every row. The posting's text is one more call per
    row, which the resolver in core/ats.py makes when the text is needed.
    """
    out: list[JobPosting] = []
    offset = 0
    while True:
        resp = _session.get(
            _with_query(url, limit=str(_SMARTRECRUITERS_PAGE), offset=str(offset)),
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        page = data.get("content") or []
        for j in page:
            org = j.get("company") or {}
            loc = j.get("location") or {}
            p = _posting(
                org.get("name") or company,
                j.get("name"),
                [loc.get("fullLocation") or _place(loc)],
                f"https://jobs.smartrecruiters.com/{org['identifier']}/{j['id']}"
                if org.get("identifier") and j.get("id")
                else None,
                _iso_ts(j.get("releasedDate")),
                raw=j,
            )
            if p:
                out.append(p)
        offset += len(page)
        if not page or offset >= int(data.get("totalFound") or 0):
            return out


# Oracle Recruiting's candidate-experience API honoured limit=200 on Nokia's
# site (617 postings) on 2026-09-05.
_ORACLE_PAGE = 200


def _oracle(url: str, company: str) -> list[JobPosting]:
    """GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?siteNumber=CX_1

    The site number on the listings URL is the one in the tenant's own posting
    URLs (/hcmUI/CandidateExperience/en/sites/{site}/job/{id}). The finder
    goes on the URL verbatim because Oracle reads its ; and , unencoded. A row
    carries a short description and, when the tenant filled them in, its
    qualifications and responsibilities; the full text is one more call per
    row, which the resolver makes.
    """
    parsed = urlparse(url)
    site = (parse_qs(parsed.query).get("siteNumber") or ["CX_1"])[0]
    endpoint = urlunparse(parsed._replace(query="", fragment=""))
    base = f"https://{parsed.netloc}/hcmUI/CandidateExperience/en/sites/{site}/job/"
    out: list[JobPosting] = []
    offset = 0
    total: int | None = None
    while True:
        finder = (
            f"findReqs;siteNumber={site},limit={_ORACLE_PAGE},offset={offset},"
            "sortBy=POSTING_DATES_DESC"
        )
        resp = _session.get(
            f"{endpoint}?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}",
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items") or [{}]
        page = items[0].get("requisitionList") or []
        for j in page:
            p = _posting(
                company,
                j.get("Title"),
                [
                    j.get("PrimaryLocation"),
                    *[s.get("Name") for s in j.get("secondaryLocations") or []],
                ],
                base + str(j["Id"]) if j.get("Id") else None,
                _iso_ts(f"{j['PostedDate']}T00:00:00+00:00") if j.get("PostedDate") else 0,
                raw=j,
            )
            if p:
                out.append(p)
        if total is None:
            total = int(items[0].get("TotalJobsCount") or 0)
        offset += len(page)
        if not page or offset >= total:
            # Oracle serves at most 10,000 rows of a search: past that the
            # page is empty and the count reads 0 (AutoZone listed 10,788,
            # Marriott 12,979, on 2026-10-05). The newest 10,000 arrive, by
            # the sort above, and the rest are unseen, not closed.
            if offset < total:
                raise PartialPull(out)
            return out


# Host -> seconds between requests from this process. apply.workable.com
# answers 429 to a burst: 143 of 172 boards failed the hour the bundle first
# pulled (2026-09-05), and six seconds was not enough where two workers share
# one egress address. The values are app_config ingest_host_pace_seconds,
# handed in by the ingest task before each pull, so a host is tuned from the
# alert rather than from a deploy.
_PACE_SECONDS: dict[str, float] = {}
_last_call: dict[str, float] = {}


def set_pace(hosts: dict) -> None:
    _PACE_SECONDS.clear()
    _PACE_SECONDS.update({str(h): float(s) for h, s in (hosts or {}).items() if s})


def _pace(host: str) -> None:
    wait = _PACE_SECONDS.get(host)
    if not wait:
        return
    ahead = _last_call.get(host, 0.0) + wait - time.monotonic()
    if ahead > 0:
        time.sleep(ahead)
    _last_call[host] = time.monotonic()


def _workable(url: str, company: str) -> list[JobPosting]:
    """POST https://apply.workable.com/api/v3/accounts/{account}/jobs

    Ten postings per reply, the next page named by a token in it; no page
    size parameter is honoured (limit, pageSize and size all tried
    2026-09-05). The posting's text is one more call per row, which the
    resolver makes.
    """
    account = urlparse(url).path.split("/accounts/", 1)[1].split("/")[0]
    body: dict = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    out: list[JobPosting] = []
    while True:
        _pace("apply.workable.com")
        resp = _session.post(url, json=body, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("results") or []
        for j in page:
            p = _posting(
                company,
                j.get("title"),
                [_place(j.get("location") or {})],
                f"https://apply.workable.com/{account}/j/{j['shortcode']}/"
                if j.get("shortcode")
                else None,
                _iso_ts(j.get("published")),
                raw=j,
            )
            if p:
                out.append(p)
        token = data.get("nextPage")
        if not page or not token:
            return out
        body = {**body, "token": token}


# jobs.apple.com answers 20 postings a page whatever size is asked (limit,
# pageSize, size, rows, perPage and count all tried 2026-10-05).
_APPLE_PAGE = 20


def _apple(url: str, company: str) -> list[JobPosting]:
    """POST https://jobs.apple.com/api/v1/search

    One row per posting and location, each its own public page at
    /en-us/details/{id}/{slug}, which is how the careers site links them. The
    row carries a summary, not the posting's text, so the text comes from the
    Apple resolver in core/fetching/ats.py.

    Sorted newest, the managed pipeline roles (evergreen retail openings) are
    stamped with the request's own time and so tie at the top, in an order
    each request draws afresh: one pass of 6,192 on 2026-10-05 returned two of
    them twice and two never, the same in each of three runs. The pages that
    held them are read again until the count is reached or a round finds
    nothing new; short of the count the pull is partial.
    """

    def page(number: int) -> tuple[list[dict], int]:
        _pace("jobs.apple.com")
        resp = _session.post(
            url,
            # Without "format" every page is empty and says totalRecords 0,
            # with a 200, which reads as an empty board. "filters" missing is
            # a 436. Both measured 2026-10-05; the body is the careers site's.
            json={
                "query": "",
                "filters": {},
                "page": number,
                "locale": "en-us",
                "sort": "newest",
                "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        res = resp.json().get("res") or {}
        return res.get("searchResults") or [], int(res.get("totalRecords") or 0)

    seen: dict[str, JobPosting] = {}

    def keep(rows: list[dict]) -> None:
        for j in rows:
            places = [
                ", ".join(dict.fromkeys(x for x in (loc.get("name"), loc.get("countryName")) if x))
                for loc in j.get("locations") or []
            ]
            p = _posting(
                company,
                j.get("postingTitle"),
                places,
                # A pipeline row's id is "PIPE-<n>" and its page is /details/<n>.
                f"https://jobs.apple.com/en-us/details/{j['id'].removeprefix('PIPE-')}"
                f"/{j['transformedPostingTitle']}"
                if j.get("id") and j.get("transformedPostingTitle")
                else None,
                # A managed role's date is the request's, not the posting's.
                0 if j.get("managedPipelineRole") else _iso_ts(j.get("postDateInGMT")),
                raw=j,
            )
            if p:
                seen[p.url] = p

    rows, total = page(1)
    unstable: list[int] = []
    number = 1
    # Every page restates the count except the empty one past the end, which
    # says 0; the first page's count is the one the pull is held to.
    while rows:
        keep(rows)
        if any(j.get("managedPipelineRole") for j in rows):
            unstable.append(number)
        if number * _APPLE_PAGE >= total:
            break
        number += 1
        rows, _ = page(number)
    while len(seen) < total and unstable:
        before = len(seen)
        for number in unstable:
            keep(page(number)[0])
        if len(seen) == before:
            break
    if len(seen) < total:
        raise PartialPull(list(seen.values()))
    return list(seen.values())


# ByteDance's careers API, which serves TikTok (lifeattiktok.com) and
# ByteDance (joinbytedance.com) alike. Either host answers for either board:
# the board is the website-path header, and without it the API answers 400
# (measured 2026-10-05). So the listings URL names the board in a website-path
# query parameter, which is sent as the header, and the public posting page is
# the one that board's own site links to.
_BYTEDANCE_HOSTS = frozenset({"api.lifeattiktok.com", "jobs.bytedance.com"})
_BYTEDANCE_SEARCH = "/api/v1/public/supplier/search/job/posts"
_BYTEDANCE_POSTING = {
    "tiktok": "https://lifeattiktok.com/search/",
    # jobs.bytedance.com/en/position/{id}/detail redirects here.
    "en": "https://joinbytedance.com/search/",
}

# The limit asked for is honoured: 5,000 returned TikTok's whole board in one
# 18 MB reply on 2026-10-05. 500 keeps a reply near 2 MB and at most about 11
# seconds (ByteDance's slowest page that day), inside TIMEOUT, and reads TikTok
# (4,289) in 9 requests rather than 43.
_BYTEDANCE_PAGE = 500

# The search serves only its first 10,000 rows: a request whose offset plus
# limit passes 10,000 returns no rows and a count of 10,000, whatever the
# board holds (both boards, 2026-10-05). No board was that large, so whether
# the first page's count also stops at 10,000 is unmeasured; a count at the
# window is read as "at least this many".
_BYTEDANCE_WINDOW = 10_000


def _bytedance(url: str, company: str) -> list[JobPosting]:
    """POST https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts?website-path=tiktok

    Every row carries the posting's text (description and requirement), so
    nothing downstream fetches the page to read it. Rows carry no posting date.
    """
    parsed = urlparse(url)
    board = (parse_qs(parsed.query).get("website-path") or [""])[0]
    if board not in _BYTEDANCE_POSTING:
        raise ValueError(f"unknown ByteDance board {board!r} in {url}")
    endpoint = urlunparse(parsed._replace(query="", fragment=""))
    seen: dict[str, JobPosting] = {}
    offset = 0
    total: int | None = None
    while True:
        resp = _session.post(
            endpoint,
            json={"limit": min(_BYTEDANCE_PAGE, _BYTEDANCE_WINDOW - offset), "offset": offset},
            headers={"website-path": board},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        reply = resp.json()
        if reply.get("code") != 0:
            raise ValueError(f"{url} answered code {reply.get('code')}: {reply.get('message')}")
        data = reply.get("data") or {}
        page = data.get("job_post_list") or []
        for j in page:
            city = j.get("city_info")
            places: list[str] = []
            while city:
                places.append(city.get("en_name") or "")
                city = city.get("parent")
            # City, state, country; a city-state names itself three times.
            location = ", ".join(dict.fromkeys(p for p in places if p))
            p = _posting(
                company,
                j.get("title"),
                [location],
                _BYTEDANCE_POSTING[board] + j["id"] if j.get("id") else None,
                0,
                raw={k: v for k, v in j.items() if k != "requirement"},
                description=join(
                    j.get("title"), location, j.get("description"), j.get("requirement")
                ),
            )
            if p:
                seen[p.url] = p
        # The count rides on every page, and is the first page's to trust:
        # one past the window reads 10,000.
        if total is None:
            total = int(data.get("count") or 0)
        offset += len(page)
        if not page or offset >= min(total, _BYTEDANCE_WINDOW):
            break
    # Paging is by offset over a live board, so a posting closed mid-pull
    # shifts the rest and one goes unseen; the full pulls on 2026-10-05 saw
    # every posting once (4,289 and 1,416 distinct).
    if len(seen) < total or total >= _BYTEDANCE_WINDOW:
        raise PartialPull(list(seen.values()))
    return list(seen.values())


# "Search Results Page 1 of 35" in the results heading is the portal's only
# statement of its size (Expleo writes "page 1 of 1"). It counts pages, not
# postings, and pr= is zero-based where the heading is not.
_ICIMS_PAGES = re.compile(r"\bpage\s+(\d+)\s+of\s+(\d+)", re.I)
# A tenant that moved to a hosted career site answers its portal with a script
# that sends the top window there (careers-spiritaero, careers-amd, 2026-10-05).
_ICIMS_MOVED = re.compile(r"window\.top\.location\.href\s*=\s*'([^']+)'")
# The labels a tenant gives its location field: GDMS "Job Location", Electric
# Boat "Location" (beside "Seat Location", a building), Joby "Job Locations".
_ICIMS_LOCATION = re.compile(r"^(job\s+)?locations?$", re.I)
_ICIMS_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _icims(url: str, company: str) -> list[JobPosting]:
    """GET https://careers-{tenant}.icims.com/jobs/search?pr={page}&in_iframe=1

    HTML, a page of 20 or 50 cards as the tenant configured it (GDMS 20,
    Electric Boat 50). The first page says how many pages there are; pr= past
    the last answers 200 with no cards. Measured on ten tenants, 2026-10-05.

    The pull is complete only if it read that many pages, every page but the
    last was as full as the first, and no posting appeared twice. A posting
    added or removed mid-pull shifts the sort under the pages, which shows as
    a repeat or a short page, and the pull is then partial.
    """
    seen: dict[str, JobPosting] = {}
    rows = 0
    complete = True
    pages = size = 0
    page = 0
    while page == 0 or page < pages:
        resp = _session.get(_with_query(url, ss="1", in_iframe="1", pr=str(page)), timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = [
            a.find_parent(class_="row") or a for a in soup.select(".iCIMS_JobsTable .title a[href]")
        ]
        stated = _ICIMS_PAGES.search(
            " ".join(h.get_text(" ") for h in soup.select(".iCIMS_SubHeader_Jobs"))
        )
        if page == 0:
            moved = _ICIMS_MOVED.search(resp.text)
            if not cards and moved:
                target = urlparse(moved.group(1).replace("\\/", "/"))
                raise ValueError(
                    f"{url} sends its visitors to {target.netloc}; "
                    f"list it as https://{target.netloc}/api/jobs"
                )
            if not cards:
                return []
            if not stated:
                raise ValueError(f"{url} lists postings but states no page count")
            pages, size = int(stated.group(2)), len(cards)
        elif not stated or int(stated.group(2)) != pages:
            complete = False
        last = page == pages - 1
        if not cards or (len(cards) != size and not last) or len(cards) > size:
            complete = False
        # Counted per card, so a card the parser cannot read also fails the
        # proof rather than vanishing from it.
        rows += len(cards)
        for card in cards:
            p = _icims_posting(card, company)
            if p:
                seen[p.url] = p
        page += 1
    postings = list(seen.values())
    if not complete or len(postings) != rows:
        raise PartialPull(postings)
    return postings


def _icims_posting(card: Tag, company: str) -> JobPosting | None:
    anchor = card.select_one(".title a[href]") or card
    heading = anchor.find("h3")
    title = heading.get_text(" ", strip=True) if heading else ""
    # Every field is a label and a value: a <dt>/<dd> pair in the card's
    # body, or an sr-only label beside a value span in its header, whose
    # title attribute holds the exact date where the text says "8 hours ago".
    fields: dict[str, str] = {}
    for tag in card.select(".iCIMS_JobHeaderTag"):
        dt, dd = tag.find("dt"), tag.find("dd")
        if dt and dd:
            fields[dt.get_text(" ", strip=True)] = dd.get_text(" ", strip=True)
    for label in card.select(".header .field-label"):
        value = label.find_next_sibling("span")
        if isinstance(value, Tag):
            text = value.get("title") or value.get_text(" ", strip=True)
            fields[label.get_text(" ", strip=True)] = str(text)
    locations = [
        place.strip()
        for label, value in fields.items()
        if _ICIMS_LOCATION.match(label)
        for place in value.split("|")
    ]
    posted = next((v for k, v in fields.items() if "posted" in k.lower()), "")
    # Month first: every US tenant measured writes 10/4/2026 for 4 October.
    day = _ICIMS_DATE.search(posted)
    href = anchor.get("href")
    return _posting(
        company,
        title,
        locations,
        str(href) if href else None,
        int(
            datetime.datetime(
                int(day.group(3)), int(day.group(1)), int(day.group(2)), tzinfo=datetime.UTC
            ).timestamp()
        )
        if day
        else 0,
        raw=fields,
    )


# Jibe answers 422 to limit=101 and above (careers.spiritaero.com, 2026-10-05).
_JIBE_PAGE = 100


def _jibe(url: str, company: str) -> list[JobPosting]:
    """GET https://{careers site}/api/jobs?limit=100&page={n}

    iCIMS's hosted career sites (Jibe). page= is one-based (page=0 is a 422),
    every page states totalCount, and a page past the end is empty. Each job
    carries its full text and names its employer. The posting a person opens
    is /jobs/{slug} on the same host, which redirects where a tenant mounts
    its site under a path (AMD's /careers-home).
    """
    host = urlparse(url).netloc
    seen: dict[str, JobPosting] = {}
    rows = total = 0
    page = 1
    while True:
        resp = _session.get(
            _with_query(url, limit=str(_JIBE_PAGE), page=str(page)), timeout=TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        jobs = [j.get("data") or {} for j in data.get("jobs") or []]
        if page == 1:
            total = int(data.get("totalCount") or 0)
        for j in jobs:
            p = _posting(
                j.get("hiring_organization") or company,
                j.get("title"),
                list(dict.fromkeys(s.strip() for s in (j.get("full_location") or "").split(";"))),
                f"https://{host}/jobs/{j['slug']}" if j.get("slug") else None,
                _iso_ts(j.get("posted_date")),
                raw={k: v for k, v in j.items() if k not in _JIBE_TEXT},
                description=_jibe_text(j),
            )
            if p:
                seen[p.url] = p
        rows += len(jobs)
        if not jobs or rows >= total:
            break
        page += 1
    postings = list(seen.values())
    if len(postings) != max(rows, total):
        raise PartialPull(postings)
    return postings


_JIBE_TEXT = ("responsibilities", "qualifications")


def _jibe_text(job: dict) -> str:
    """The description, which on most tenants already holds the
    responsibilities and qualifications; V2X left them out of 17 of 765."""
    body = job.get("description") or ""
    extra = [job.get(k) or "" for k in _JIBE_TEXT]
    return clean_html(join(body, *[e for e in extra if e.strip() and e not in body]))


# The feed returns its first `rows` postings and nothing more: it has no
# paging (startrow, start, page and offset were all ignored on 2026-10-05), and
# without `rows` it returns 20. Asked for more than a board holds it returns
# them all, measured to 2,233 of 2,233 on L3Harris, the largest board found.
# Whether it stops silently somewhere above that is unmeasured, which is why
# completeness is read off the sitemap and not off this number.
_SUCCESSFACTORS_ROWS = 100_000


def _successfactors_heading(heading: str) -> tuple[str, str]:
    """Title and primary location from a feed item's "<title> (<location>)".

    Either half can carry parentheses of its own, so the location is the
    balanced group that closes the heading: "Sr Spec, Quality Engrg (Supplier
    Quality) (Rochester, NY, US, 14623)" on L3Harris, "Stagiaire Ingénieur
    Test et Mesure (F/H) (Arc Les Gray Cedex, Saône (Haute), FR, 70103)" on
    Deere, "Senior Enterprise Account Executive FSI (Southbank (Melbourne),
    VIC, AU, 3006)" on SAP, where 69 of 877 locations nest one. A heading that does not close on
    a group is all title.
    """
    text = heading.strip()
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(text[i], 0)
        if depth == 0:
            if i and text[i] == "(" and text[:i].strip():
                return text[:i].strip(), text[i + 1 : -1].strip()
            break
    return text, ""


_ROOT_TAG = re.compile(rb"<[A-Za-z]")


def _xml(raw: bytes) -> ET.Element:
    """Parse a document a board serves, refusing one that declares a DOCTYPE.

    ElementTree expands internal entities, so a DOCTYPE is how a billion-laughs
    document hangs a worker. Neither the sitemap nor the feed declares one (six
    boards, 2026-10-05). A DOCTYPE can only stand before the root element, so
    the prolog is all that needs reading; the same rule as the mail importer's.
    """
    root = _ROOT_TAG.search(raw)
    if b"<!DOCTYPE" in raw[: root.start() if root else len(raw)].upper():
        raise ValueError("refusing an XML document with a DOCTYPE")
    return ET.fromstring(raw)  # noqa: S314 - DOCTYPE refused above


def _successfactors_id(link: str) -> str:
    """The requisition id, the last segment of /job/<slug>/<id>/."""
    return urlparse(link).path.rstrip("/").rsplit("/", 1)[-1]


def _successfactors(url: str, company: str) -> list[JobPosting]:
    """GET https://{host}/services/rss/job/ on a SuccessFactors Career Site Builder site.

    One call carries every posting with its text and date, across all of the
    site's languages (Hensoldt's search pages said 535 in English and 512 in
    German; the feed held 1,051). The feed states no total, so the site's
    /sitemap.xml, which lists the same /job/ urls, is the count: on 2026-10-05
    the two held identical requisition ids on nine boards, from 115 (ULA) to
    2,233 (L3Harris). A requisition in the sitemap and not in the feed
    makes the pull partial. The sitemap is read first, so a posting opened
    between the two calls only adds to the feed, and one closed between them
    costs a partial pull, never a wrong retirement.
    """
    parsed = urlparse(url)
    sitemap = _session.get(f"{parsed.scheme}://{parsed.netloc}/sitemap.xml", timeout=TIMEOUT)
    sitemap.raise_for_status()
    root = _xml(sitemap.content)
    # Two shapes, each listing every posting url. A urlset of <loc>s, whose
    # namespace is not the standard one everywhere (ULA's is
    # http://www.google.com/schemas/sitemap/0.9), so the local name is
    # matched. Or a Google Base RSS feed of every posting, read by its <link>s:
    # SAP, Deere, Halliburton and Boston Scientific serve that, and on the
    # last three its ids equalled the feed's (337, 442, 548). A sitemap index
    # has never been seen on one of these sites; its <loc>s name further
    # sitemaps, so it, or any other shape, cannot prove the pull complete.
    shape = root.tag.rsplit("}", 1)[-1]
    complete = shape in ("urlset", "rss")
    link = "loc" if shape == "urlset" else "link"
    listed = {
        _successfactors_id(e.text or "")
        for e in root.iter()
        if e.tag.rsplit("}", 1)[-1] == link and "/job/" in (e.text or "")
    }

    # The feed answers 406 to the session's JSON Accept and to application/xml.
    resp = _session.get(
        _with_query(url, rows=str(_SUCCESSFACTORS_ROWS)),
        headers={"Accept": "application/rss+xml"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    feed = _xml(resp.content)
    # A malformed query answers 200 with <xml>Error: There is a problem with a
    # jobs query</xml> (keywords=() on ULA, 2026-10-05): a broken fetch, not
    # an empty board.
    if feed.tag != "rss":
        raise ValueError(f"not an RSS feed: {resp.content[:200]!r}")
    out: list[JobPosting] = []
    seen: set[str] = set()
    for item in feed.iterfind("./channel/item"):
        link = item.findtext("link") or ""
        # The link carries feedId and utm_ parameters; the posting is the path.
        public = urlunparse(urlparse(link)._replace(query="", fragment="")) if link else None
        heading = item.findtext("title") or ""
        title, location = _successfactors_heading(heading)
        pub = item.findtext("pubDate")
        p = _posting(
            company,
            title,
            [location],
            public,
            int(parsedate_to_datetime(pub).timestamp()) if pub else 0,
            raw={"title": heading, "link": link, "pubDate": pub},
            description=join(title, location, clean_html(item.findtext("description") or "")),
        )
        if p:
            out.append(p)
            seen.add(_successfactors_id(link))
    if not complete or listed - seen:
        raise PartialPull(out)
    return out


# Eightfold's two careers-site generations. A tenant answers one and refuses
# the other with 403 ("Not authorized for PCSX" on v2, "PCSX is not enabled"
# on pcsx), so the listings URL names the one its tenant serves.
_EIGHTFOLD_PCSX = "/api/pcsx/search"
_EIGHTFOLD_V2 = "/api/apply/v2/jobs"
# One pace for every tenant, whatever domain it serves from: one AWS WAF fronts
# them, and once it challenged an address on 2026-10-05 Lockheed, Northrop,
# CACI, PayPal and Netflix all answered 405 with x-amzn-waf-action: captcha
# for a few minutes (Microsoft's did not). See ingest_host_pace_seconds.
_EIGHTFOLD_PACE = "eightfold.ai"


def _eightfold(url: str, company: str) -> list[JobPosting]:
    """GET https://{host}/api/pcsx/search?domain={domain}
    or  https://{host}/api/apply/v2/jobs?domain={domain}

    Both page by `start` at ten postings whatever `num` asks for, state the
    count on every page, take any offset and answer an empty page past the
    end; no result window was found up to 21,774 (Starbucks, 2026-10-05).
    Neither list carries the posting's text.

    The order is not stable from one request to the next, so a pull reads
    some postings twice and others never. A posting re-dated or removed
    mid-pull shifts every later page (Microsoft, whose count moved between
    2,297 and 2,300: 2,256 distinct), and the order moves with the count
    unchanged too (Qualcomm, 2,052 throughout: 2,009 distinct in one pull and
    2,043 in the next), all on 2026-10-05. A pull is complete only when it
    holds as many distinct postings as the largest count any page stated;
    anything short is PartialPull.
    """
    parsed = urlparse(url)
    pcsx = parsed.path.rstrip("/") == _EIGHTFOLD_PCSX
    origin = f"{parsed.scheme}://{parsed.netloc}"
    seen: dict[str, JobPosting] = {}
    stated = 0
    start = 0
    while True:
        _pace(_EIGHTFOLD_PACE)
        resp = _session.get(_with_query(url, start=str(start)), timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        data = (data.get("data") or {}) if pcsx else data
        page = data.get("positions") or []
        stated = max(stated, int(data.get("count") or 0))
        for j in page:
            link = j.get("positionUrl") if pcsx else j.get("canonicalPositionUrl")
            p = _posting(
                company,
                j.get("name"),
                j.get("locations") or [j.get("location")],
                urljoin(origin, link) if link else None,
                int((j.get("postedTs") if pcsx else j.get("t_create")) or 0),
                raw=j,
            )
            if p:
                seen[p.url] = p
        if not page:
            break
        start += len(page)
    postings = list(seen.values())
    if len(postings) < stated:
        raise PartialPull(postings)
    return postings


def _place(loc: dict) -> str:
    """City, region, country as the ATS spells them, skipping what is unset."""
    return ", ".join(str(loc[k]) for k in ("city", "region", "country") if loc.get(k))


_TALEO_SECTION = re.compile(r"^/careersection/([^/]+)/jobsearch\.ftl$")


def _taleo(url: str, company: str) -> list[JobPosting]:
    """POST https://{tenant}.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}

    The listings URL is the careersection's own search page,
    /careersection/{section}/jobsearch.ftl?lang=en&portal={portal}: the
    section names the public posting URL and the portal is what the search
    endpoint takes. A URL without the portal costs one GET of that page to
    read it (`portalNo`). No cookie, session or CSRF token is needed; the
    endpoint answers 500 unless a `tz` or `tzname` header is present, and its
    value changed nothing on Bell's board (2026-10-05).

    Pages are 25 rows. The body takes a pageSize and echoes it back, but the
    server caches a page by its query and number and not by its size, for
    some minutes and across requests that share no cookie: Textron listed
    691 postings at 25, 477 at 50, 496 at 100 and 250 at 200, and a pull at
    25 straight after saw 100-row pages and 616 postings (2026-10-05). So
    no size is sent, the page count comes from the reply, and postings are
    keyed by url so a repeated row counts once. Every page repeats the same
    totalCount, and a page past the last returns the last page again
    (Kautex, 2026-10-05: pages 5, 10, 50 and 500 all held one posting), so
    the loop is bounded by the count and never by an empty page.

    Pages come back short: the count includes requisitions the list never
    renders. On Bell they were the same 22 under twelve sort orders and every
    job-type slice. Measured on 2026-10-05: Textron 691 of 751, Bell 111 of
    133, Kautex 96 of 104, Textron Aviation 97 of 106, AAR 187 of 193. A
    public posting can also be missing from the search altogether (Bell
    338801 had a live detail page and no keyword search found it). So a pull
    short of its count raises PartialPull and retires nothing.

    Rows carry no text and no company; which column is the title, the
    locations and the date is set per careersection (BAE's carries the title
    alone).
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    lang = (query.get("lang") or ["en"])[0]
    section = _TALEO_SECTION.match(parsed.path)
    if not section:
        raise ValueError(f"{url} is not a careersection search page")
    portal = (query.get("portal") or [""])[0] or _taleo_portal(url)
    endpoint = f"https://{parsed.netloc}/careersection/rest/jobboard/searchjobs"
    detail = f"https://{parsed.netloc}/careersection/{section.group(1)}/jobdetail.ftl?job="
    seen: dict[str, JobPosting] = {}
    page_no = total = last = 1
    while True:
        _pace(parsed.netloc)
        resp = _session.post(
            f"{endpoint}?lang={lang}&portal={portal}",
            json={
                "multilineEnabled": False,
                "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                "fieldData": {
                    "fields": {"KEYWORD": "", "LOCATION": "", "ORGANIZATION": ""},
                    "valid": True,
                },
                "filterSelectionParam": {"searchFilterSelections": []},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
                "pageNo": page_no,
            },
            headers={"tz": "GMT+00:00"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if page_no == 1:
            paging = data["pagingData"]
            total = int(paging["totalCount"])
            last = -(-total // int(paging["pageSize"]))
        for j in data.get("requisitionList") or []:
            columns = j.get("column") or []
            linked = j.get("linkedColumn", 0)
            located = j.get("locationsColumns") or []
            places = [p for i in located if i < len(columns) for p in _taleo_places(columns[i])]
            dates = [
                _taleo_date(c, lang)
                for i, c in enumerate(columns)
                if i != linked and i not in located
            ]
            p = _posting(
                company,
                columns[linked] if linked < len(columns) else None,
                places,
                detail + j["contestNo"] if j.get("contestNo") else None,
                next((d for d in dates if d), 0),
                raw=j,
            )
            if p:
                seen[p.url] = p
        if page_no >= last:
            break
        page_no += 1
    postings = list(seen.values())
    if len(postings) < total:
        raise PartialPull(postings)
    return postings


def _taleo_portal(url: str) -> str:
    resp = _session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    match = re.search(r"portalNo: '(\d+)'", resp.text)
    if not match:
        raise ValueError(f"{url} carries no portal: not a faceted-search careersection")
    return match.group(1)


def _taleo_places(cell: str) -> list[str]:
    """A locations cell is a JSON list in a string: '["US-Kansas-Wichita"]'."""
    try:
        places = json.loads(cell)
    except ValueError:
        return [cell]
    return [str(p) for p in places] if isinstance(places, list) else [str(places)]


def _taleo_date(cell: str, lang: str) -> int:
    """The posting date as the careersection's locale writes it: 10/02/2026
    on Textron and Oct 5, 2026 on AAR, both lang=en. Slashes are read
    month first only for en, the one locale measured."""
    text = cell.strip()
    if lang == "en" and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", text):
        form = "%m/%d/%Y"
    elif re.fullmatch(r"[A-Za-z]{3} \d{1,2}, \d{4}", text):
        form = "%b %d, %Y"
    else:
        return 0
    try:
        return int(datetime.datetime.strptime(f"{text} +0000", f"{form} %z").timestamp())
    except ValueError:
        return 0


# IBM's search API answers 400 to a size above 100 (200 and 1,000 tried,
# 2026-10-05).
_IBM_PAGE = 100

# The careers index names its facets by slot. Measured 2026-10-05 against the
# careers search page's own filters: 05 country, 08 area of work, 17 work
# arrangement, 18 position type, 19 "City, CC" or "Multiple Cities", and
# field_text_01 the job id in the posting url. The API refuses a _source
# naming any field outside its allow list, so these are asked for by name.
_IBM_FIELDS = [
    "title",
    "url",
    "field_text_01",
    "field_keyword_05",
    "field_keyword_08",
    "field_keyword_17",
    "field_keyword_18",
    "field_keyword_19",
]


def _ibm(url: str, company: str) -> list[JobPosting]:
    """POST https://www-api.ibm.com/search/api/v2, the search behind
    www.ibm.com/careers/search.

    Pages by search_after on _id. Paging by from repeats rows: the site's own
    sort ties on every posting of an empty search, and 20 shards break the tie
    differently per request, so a from/size walk of 2,002 postings returned
    1,890 distinct on 2026-10-05. A from walk also stops at from+size 10,000.
    The cursor reads the index to its end, and the first page's count is
    then only the check that nothing was lost on the way.

    No text is carried. `description` is a 256-character snippet, and `body`
    is the posting's prose without its headings, education or years of
    experience (134730: 3,668 characters of a 7,663-character page), which
    stored as the posting would read as complete and be judged short. The
    posting page is fetched for the text like any other.
    """
    body: dict[str, Any] = {
        "appId": "careers",
        "scopes": ["careers2"],
        "query": {"bool": {"must": []}},
        "lang": "zz",
        "localeSelector": {},
        "sm": {"query": "", "lang": "zz"},
        "_source": _IBM_FIELDS,
        "size": _IBM_PAGE,
        "sort": [{"_id": "asc"}],
    }
    endpoint = urlunparse(urlparse(url)._replace(query="", fragment=""))
    out: list[JobPosting] = []
    total: int | None = None
    while True:
        resp = _session.post(endpoint, json=body, timeout=TIMEOUT)
        resp.raise_for_status()
        hits = resp.json()["hits"]
        page = hits.get("hits") or []
        if total is None:
            total = int((hits.get("total") or {}).get("value") or 0)
        for h in page:
            j = h.get("_source") or {}
            place = j.get("field_keyword_19")
            p = _posting(
                company,
                j.get("title"),
                # "Multiple Cities" is no more a place than Workday's "2
                # Locations" (349 of 2,013 on 2026-10-05); the country is.
                [j.get("field_keyword_05") if place == "Multiple Cities" else place],
                j.get("url"),
                0,
                raw=j,
            )
            if p:
                out.append(p)
        if len(page) < _IBM_PAGE:
            break
        body["search_after"] = page[-1]["sort"]
    if len(out) < total:
        raise PartialPull(out)
    return out


# Goldman's roleSearch refuses a pageSize above 250 ("must be less than or
# equal to 250", 2026-10-05).
_GOLDMAN_PAGE = 250

# The experiences a person outside the firm can apply to. The schema's fourth,
# INTERNAL_MOBILITY, is for employees. The careers site's results page asks
# for the first two (715 roles on 2026-10-05) and its students page for
# CAMPUS (238), which is where the internships and analyst programs are.
_GOLDMAN_EXPERIENCES = ["EARLY_CAREER", "PROFESSIONAL", "CAMPUS"]

_GOLDMAN_QUERY = """query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items {
      roleId jobTitle corporateTitle jobFunction division status lastPostedDate
      locations { primary city state country }
      compensation { minSalary maxSalary currency }
      descriptionHtml
      externalSource { sourceId }
    }
  }
}"""


def _goldman(url: str, company: str) -> list[JobPosting]:
    """POST https://api-higher.gs.com/gateway/api/v1/graphql, the roleSearch
    behind higher.gs.com, unauthenticated.

    Pages are numbered from 0. A refused request is HTTP 200 with `errors`
    and no data, which must fail the pull rather than read as an empty board.
    The role's public page is /roles/{externalSource.sourceId}; the roleId
    carries a suffix (_GS_MID_CAREER, _GS_CAMPUS, or a uuid) the page does not.
    """
    out: list[JobPosting] = []
    seen: set[str] = set()
    total: int | None = None
    number = 0
    while True:
        resp = _session.post(
            url,
            json={
                "operationName": "GetRoles",
                "query": _GOLDMAN_QUERY,
                "variables": {
                    "searchQueryInput": {
                        "page": {"pageSize": _GOLDMAN_PAGE, "pageNumber": number},
                        "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"},
                        "filters": [],
                        "experiences": _GOLDMAN_EXPERIENCES,
                        "searchTerm": "",
                    }
                },
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"roleSearch refused: {data['errors']}")
        result = data["data"]["roleSearch"]
        page = result.get("items") or []
        if total is None:
            total = int(result.get("totalCount") or 0)
        for j in page:
            source_id = (j.get("externalSource") or {}).get("sourceId")
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            p = _posting(
                company,
                j.get("jobTitle"),
                [goldman_place(loc) for loc in j.get("locations") or []],
                f"https://higher.gs.com/roles/{source_id}",
                _iso_ts(j.get("lastPostedDate")),
                raw=j,
                description=goldman_text(j) if j.get("descriptionHtml") else "",
            )
            if p:
                out.append(p)
        number += 1
        if len(page) < _GOLDMAN_PAGE:
            break
    # Newest first, so a role posted mid-pull pushes rows onto later pages
    # and is read twice rather than skipped; a role closed mid-pull pulls one
    # past the page boundary unseen, and the count catches it.
    if len(seen) < total:
        raise PartialPull(out)
    return out


# amazon.jobs answers "Result limit cannot be greater than 100" past 100 a
# request, and refuses a page reaching past row 10,000 of a search ("Cannot
# return more than 10000 results at once", with HTTP 200, hits 0 and no jobs).
# Its `hits` stops at the window too: the unfiltered search said 10,000 for a
# board of 22,295 on 2026-10-05.
_AMAZON_PAGE = 100
_AMAZON_WINDOW = 10_000

# The row's text, kept out of raw for the reason _TEXT_FIELDS gives.
_AMAZON_TEXT = frozenset(
    {"description", "description_short", "basic_qualifications", "preferred_qualifications"}
)


def _amazon(url: str, company: str) -> list[JobPosting]:
    """GET https://www.amazon.jobs/en/search.json

    The board is past the window, so it is read one job category at a time.
    Categories partition it: on 2026-10-05 their counts summed to 22,295, the
    same as the country facet's, and the largest held 3,279. A pull is complete
    only when the categories account for every posting the country facet
    counts, no category reaches the window, and each category's pages yielded
    as many distinct postings as its first page stated; otherwise it is partial.
    """
    first = _amazon_get(
        url, {"result_limit": 1, "facets[]": ["category", "normalized_country_code"]}
    )
    categories = _amazon_facet(first, "category_facet")
    complete = sum(categories.values()) >= sum(
        _amazon_facet(first, "normalized_country_code_facet").values()
    )
    seen: dict[str, JobPosting] = {}
    for category in categories:
        part, whole = _amazon_slice(url, company, category)
        complete = complete and whole
        seen.update((p.url, p) for p in part)
    if not complete:
        raise PartialPull(list(seen.values()))
    return list(seen.values())


def _amazon_get(url: str, params: dict) -> dict:
    resp = _session.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"amazon.jobs refused {params}: {data['error']}")
    return data


def _amazon_facet(data: dict, name: str) -> dict[str, int]:
    """A facet as {value: count}; amazon.jobs sends it as one-key objects."""
    return {
        k: int(v) for entry in (data.get("facets") or {}).get(name) or [] for k, v in entry.items()
    }


def _amazon_slice(url: str, company: str, category: str) -> tuple[list[JobPosting], bool]:
    """One category's postings, and whether they are every one it stated."""
    seen: dict[str, JobPosting] = {}
    offset = 0
    total: int | None = None
    while True:
        data = _amazon_get(
            url,
            {
                "result_limit": min(_AMAZON_PAGE, _AMAZON_WINDOW - offset),
                "offset": offset,
                "category[]": category,
                "sort": "recent",
            },
        )
        page = data.get("jobs") or []
        for j in page:
            p = _posting(
                company,
                j.get("title"),
                [json.loads(x).get("normalizedLocation") for x in j.get("locations") or []]
                or [j.get("normalized_location")],
                f"https://www.amazon.jobs{j['job_path']}" if j.get("job_path") else None,
                posted_ts(j.get("posted_date") or ""),
                raw={k: v for k, v in j.items() if k not in _AMAZON_TEXT},
                description=_amazon_text(j),
            )
            if p:
                seen[p.url] = p
        if total is None:
            total = int(data.get("hits") or 0)
        offset += len(page)
        if not page or offset >= min(total, _AMAZON_WINDOW):
            # A posting that closes mid-read moves every later row up one, so
            # a page boundary skips an open posting; that shows as fewer
            # distinct postings than the first page's count. One that opens
            # moves them down and repeats a row, which loses nothing.
            return list(seen.values()), total < _AMAZON_WINDOW and len(seen) >= total


def _amazon_text(j: dict) -> str:
    sections = (
        ("Basic qualifications", clean_html(j.get("basic_qualifications") or "")),
        ("Preferred qualifications", clean_html(j.get("preferred_qualifications") or "")),
    )
    return join(
        j.get("title"),
        j.get("normalized_location"),
        clean_html(j.get("description") or ""),
        *[join(heading, text) for heading, text in sections if text],
    )


# --- markdown tables ---------------------------------------------------------
#
# The aggregators (speedyapply, jobright-ai, vanshb03, SimplifyJobs READMEs)
# all publish one shape: a pipe table headed by a Company column, a title
# column, a Location column, sometimes a separate link column, and a date
# column that is either an age ("5d") or a day ("Sep 03"). Which column is
# which is read from the header row, so a new aggregator that keeps the
# conventions needs no code. A "↳" company means "same as the row above".

_TITLE_HEADERS = ("position", "job title", "role")
_DATE_HEADERS = ("age", "date posted", "date")
_LINK = re.compile(r'href="([^"]+)"|\]\((https?://[^)\s]+)\)')
_TAGS = re.compile(r"<[^>]+>")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _text(cell: str) -> str:
    return ftfy.fix_text(_TAGS.sub("", _MD_LINK.sub(r"\1", cell)).replace("**", "")).strip()


def _link(cell: str) -> str:
    m = _LINK.search(cell)
    return (m.group(1) or m.group(2)) if m else ""


def parse_markdown(text: str) -> list[JobPosting]:
    out: list[JobPosting] = []
    columns: dict[str, int] = {}
    company = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        heads = [_text(c).lower() for c in cells]
        if heads and heads[0] == "company":
            columns = {}
            for i, h in enumerate(heads):
                if h in _TITLE_HEADERS:
                    columns["title"] = i
                elif h == "location":
                    columns["location"] = i
                elif h in _DATE_HEADERS:
                    columns["date"] = i
                elif h == "posting" or h.startswith("appl"):
                    columns["link"] = i
            continue
        if not columns or "title" not in columns or set(cells[0]) <= {"-", ":"}:
            continue
        cell = {k: cells[i] for k, i in columns.items() if i < len(cells)}
        company = _text(cells[0]) if _text(cells[0]) != "↳" else company
        url = _link(cell.get("link", "")) or _link(cell.get("title", ""))
        location = _text(cell.get("location", "").replace("</br>", "; "))
        location = re.sub(r"\s*\+\d+\s*$", "", location)
        p = _posting(
            company,
            _text(cell.get("title", "")),
            location.split(";"),
            url,
            posted_ts(cell.get("date", "")),
        )
        if p:
            out.append(p)
    return out


def _markdown(url: str, company: str) -> list[JobPosting]:
    resp = _session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return parse_markdown(resp.text)


# --- dates -------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_DAY = 86400


def _iso_ts(value: str | None) -> int:
    if not value:
        return 0
    return int(datetime.datetime.fromisoformat(value).timestamp())


def posted_ts(text: str, now: datetime.datetime | None = None) -> int:
    """Epoch seconds for the ways boards write a posting date, or 0 when the
    text does not say. 0 is "unknown", and the catalog stores it as NULL.

    "Posted 30+ Days Ago" is unknown too: Workday says only that it is older
    than the window, and the hourly cycle sees every posting inside the window
    on its first appearance anyway, so the only loss is the backfill when a
    board is first added.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    t = text.strip().lower()
    if t in ("today", "posted today"):
        return int(now.timestamp())
    if t in ("yesterday", "posted yesterday"):
        return int(now.timestamp()) - _DAY
    # "5d", "2mo", "posted 14 days ago"; the unit is its first letter except
    # months, which must not read as minutes.
    m = re.match(r"(?:posted\s+)?(\d+)(\+?)\s*(mo|[hdwy])", t)
    if m:
        if m.group(2):
            return 0
        amount, unit = int(m.group(1)), m.group(3)
        days = {
            "h": amount / 24,
            "d": amount,
            "w": amount * 7,
            "mo": amount * 30,
            "y": amount * 365,
        }
        return int(now.timestamp() - days[unit] * _DAY)
    m = re.match(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?$", t)
    if m and m.group(1) in _MONTHS:
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            day = datetime.datetime(year, _MONTHS[m.group(1)], int(m.group(2)), tzinfo=datetime.UTC)
        except ValueError:
            return 0
        # A day without a year is the most recent one that is not in the
        # future; a feed updated daily never writes tomorrow.
        if not m.group(3) and day > now + datetime.timedelta(days=1):
            day = day.replace(year=year - 1)
        return int(day.timestamp())
    return 0
