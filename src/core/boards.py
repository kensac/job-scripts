"""Listing fetchers: one per board format, chosen by the listings URL.

A source is a URL, so the format is read off the URL rather than stored beside
it. The applicant-tracking systems publish their boards on hosts of their own,
and the GitHub aggregators publish a markdown table or a JSON file. Every
fetcher returns the same JobPosting, so ingest, the catalog and the checks
never learn which board a posting came from.

A company board lists every opening, most of them senior. Ingest applies the
source's title_pattern before anything downstream sees the postings, because
verify_new checks every active posting with cached text regardless of who
subscribed: SpaceX listed 2,309 openings on 2026-09-04, of which 68 read as
entry level, and without the pattern the other 2,241 would each cost a closed
and a clearance check.
"""

from __future__ import annotations

import datetime
import logging
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import ftfy
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.ats import ashby_text, greenhouse_text, lever_text
from core.pittcsc_simplify import JobPosting, fetch_job_postings
from core.urls import normalize_url

logger = logging.getLogger(__name__)

TIMEOUT = 30.0

# The hourly cycle is the real retry; this only rides out a blip inside one
# fetch, which the markdown fetcher it replaces also did (three attempts).
_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain"})
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
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
    if parsed.path.endswith(".md"):
        return "markdown"
    return "sheet_era"


# Lever, Ashby and Workday list a company's own openings and never say whose;
# Greenhouse names the company on every job and the aggregators name it per
# row.
NEEDS_COMPANY = frozenset({"lever", "ashby", "workday"})

# A company's own board lists every open posting, so a posting missing from
# it is closed. An aggregator list trims old rows on its own schedule, so
# absence there says nothing; those postings close through the reverify
# sweep instead.
AUTHORITATIVE = frozenset({"greenhouse", "lever", "ashby", "workday"})


def fetch_listings(url: str, company: str | None = None) -> list[JobPosting]:
    fetcher = {
        "greenhouse": _greenhouse,
        "lever": _lever,
        "ashby": _ashby,
        "workday": _workday,
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
        description=description,
        raw={k: v for k, v in (raw or {}).items() if k not in _TEXT_FIELDS},
    )


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


def _workday(url: str, company: str) -> list[JobPosting]:
    """POST https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

    A searchText query parameter on the listings URL becomes the search the
    tenant's own careers page would run, which is the only server-side filter
    these boards offer; Boeing returned 334 postings for "new grad" on 2026-09-04.
    """
    parsed = urlparse(url)
    search = (parse_qs(parsed.query).get("searchText") or [""])[0]
    endpoint = urlunparse(parsed._replace(query="", fragment=""))
    site = parsed.path.split("/wday/cxs/", 1)[1].split("/")[1]
    base = f"https://{parsed.netloc}/{site}"
    out: list[JobPosting] = []
    offset = 0
    while True:
        resp = _session.post(
            endpoint,
            json={
                "appliedFacets": {},
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
                [j.get("locationsText")],
                base + j.get("externalPath", "") if j.get("externalPath") else None,
                posted_ts(j.get("postedOn") or ""),
            )
            if p:
                out.append(p)
        offset += len(page)
        if not page or offset >= int(data.get("total") or 0):
            return out


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
