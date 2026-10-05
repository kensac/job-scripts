from __future__ import annotations

import datetime
import enum
import html
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import ClassVar, TypeGuard
from urllib.parse import parse_qs, unquote, urlparse

import ftfy
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TIMEOUT = 20.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})


class Status(enum.Enum):
    OK = "ok"
    GONE = "gone"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


@dataclass(frozen=True)
class AtsResult:
    status: Status
    text: str | None = None
    source: str | None = None
    # The day the board says the posting went up, when the API states one.
    # Workday's list endpoint says only "Posted 30+ Days Ago" past its window,
    # but the posting's own JSON carries the start date, so 1,154 undated rows
    # on 2026-09-04 could be dated from the fetch they were about to get.
    posted: datetime.date | None = None

    @property
    def ok(self) -> bool:
        return self.status is Status.OK


UNSUPPORTED = AtsResult(Status.UNSUPPORTED)


def source_posting_url(url: str, listings_url: str | None) -> str:
    """An embedded posting's catalog source supplies its board identity.

    A hostname guess cannot prove a closure: a wrong board and a removed
    posting both return 404. Only use the configured Greenhouse API source.
    Keep the original URL as the storage key and browser fallback.
    """
    posting = urlparse(url)
    job_id = (parse_qs(posting.query).get("gh_jid") or [None])[0]
    listing = urlparse(listings_url or "")
    board = re.fullmatch(r"/v1/boards/([A-Za-z0-9_-]+)/jobs/?", listing.path)
    if (
        job_id
        and job_id.isascii()
        and job_id.isdecimal()
        and listing.hostname == "boards-api.greenhouse.io"
        and board
    ):
        return f"https://boards.greenhouse.io/{board.group(1)}/jobs/{job_id}"
    return url


def clean_html(raw: str) -> str:
    text = BeautifulSoup(html.unescape(raw or ""), "html.parser").get_text("\n")
    return ftfy.fix_text(re.sub(r"\n{3,}", "\n\n", text)).strip()


def _iso_date(text: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def join(*parts: str | None) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip()).strip()


# The text of one posting from the JSON its board's API returns. One function
# per format, shared by the resolver (which fetches a posting by URL) and the
# listing fetcher in core/boards.py (which gets the same JSON for every
# posting in one call), so the text a listing stores is exactly the text a
# resolver would have fetched for it, and the per-posting fetch is not needed.


def greenhouse_text(data: dict) -> str:
    return join(
        data.get("title"),
        (data.get("location") or {}).get("name"),
        clean_html(data.get("content", "")),
    )


def lever_text(data: dict) -> str:
    lists = [join(s.get("text"), clean_html(s.get("content", ""))) for s in data.get("lists", [])]
    return join(
        data.get("text"),
        (data.get("categories") or {}).get("location"),
        clean_html(data.get("description", "")),
        *lists,
        clean_html(data.get("additional", "")),
    )


def ashby_text(job: dict) -> str:
    comp = (job.get("compensation") or {}).get("compensationTierSummary")
    return join(
        job.get("title"),
        job.get("location"),
        comp,
        clean_html(job.get("descriptionHtml", "")),
    )


def goldman_text(role: dict) -> str:
    """One role as Goldman's roleSearch returns it. The posting page shows the
    same description beside its overview (title, place, corporate title, pay)
    and a block of firm-wide benefits, which is left out."""
    comp = role.get("compensation") or {}
    pay = ""
    if comp.get("minSalary") and comp.get("maxSalary"):
        pay = f"{comp.get('currency') or ''} {comp['minSalary']:,.0f} - {comp['maxSalary']:,.0f}"
    return join(
        role.get("jobTitle"),
        "; ".join(goldman_place(loc) for loc in role.get("locations") or []),
        role.get("corporateTitle"),
        pay,
        clean_html(role.get("descriptionHtml") or ""),
    )


def goldman_place(loc: dict) -> str:
    return ", ".join(str(loc[k]) for k in ("city", "state", "country") if loc.get(k))


def oracle_text(item: dict) -> str:
    """One requisition as Oracle Recruiting's detail call returns it. The
    listing call carries the same keys minus the description, so a listing
    row would read short; the resolver fetches the detail instead."""
    return join(
        item.get("Title"),
        item.get("PrimaryLocation"),
        clean_html(item.get("ExternalDescriptionStr") or ""),
        clean_html(item.get("ExternalResponsibilitiesStr") or ""),
        clean_html(item.get("ExternalQualificationsStr") or ""),
    )


def workable_text(data: dict) -> str:
    loc = data.get("location") or {}
    return join(
        data.get("title"),
        ", ".join(str(loc[k]) for k in ("city", "region", "country") if loc.get(k)),
        clean_html(data.get("description") or ""),
        clean_html(data.get("requirements") or ""),
        clean_html(data.get("benefits") or ""),
    )


class AtsResolver(ABC):
    name: ClassVar[str]
    markers: ClassVar[tuple[str, ...]]

    def matches(self, url: str) -> bool:
        return any(m in url for m in self.markers)

    @abstractmethod
    def fetch(self, url: str) -> AtsResult: ...

    def canonical(self, url: str) -> str | None:
        """Collapse URL variants of one posting onto a single clickable URL."""
        return None

    def get(self, url: str) -> requests.Response | None:
        try:
            return _session.get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            logger.debug(f"[{self.name}] request failed {url}: {exc}")
            return None

    def result(self, text: str | None) -> AtsResult:
        if text:
            return AtsResult(Status.OK, text, self.name)
        return AtsResult(Status.ERROR, source=self.name)

    def from_response(self, resp: requests.Response | None) -> AtsResult | None:
        if resp is None:
            return AtsResult(Status.ERROR, source=self.name)
        if resp.status_code in (404, 410):
            return AtsResult(Status.GONE, source=self.name)
        if resp.status_code != 200:
            return AtsResult(Status.ERROR, source=self.name)
        return None


class Greenhouse(AtsResolver):
    name = "greenhouse"
    markers = ("greenhouse.io", "gh_jid=")

    def canonical(self, url: str) -> str | None:
        parsed = urlparse(url)
        match = re.search(r"greenhouse\.io/([^/?]+)/jobs/(\d+)", url)
        if match:
            return f"https://{parsed.netloc.lower()}/{match.group(1)}/jobs/{match.group(2)}"
        job_id = (parse_qs(parsed.query).get("gh_jid") or [None])[0]
        if job_id:
            return f"https://{parsed.netloc.lower()}{parsed.path.rstrip('/')}?gh_jid={job_id}"
        return None

    def fetch(self, url: str) -> AtsResult:
        parsed = urlparse(url)
        board = job_id = None
        match = re.search(r"greenhouse\.io/([^/?]+)/jobs/(\d+)", url)
        if match:
            board, job_id = match.group(1), match.group(2)
        else:
            job_id = (parse_qs(parsed.query).get("gh_jid") or [None])[0]
        if not job_id:
            return UNSUPPORTED

        host = parsed.netloc.replace("www.", "").split(".")[0]
        candidates = [c for c in dict.fromkeys([board, host, host.replace("-", "")]) if c]

        last: AtsResult = AtsResult(Status.ERROR, source=self.name)
        for cand in candidates:
            resp = self.get(
                f"https://boards-api.greenhouse.io/v1/boards/{cand}/jobs/{job_id}?content=true"
            )
            early = self.from_response(resp)
            if early is not None:
                if cand == board:
                    # An explicit board identity owns the answer. Trying a
                    # hostname guess afterwards discarded authoritative 404s.
                    return early
                # A 404 only proves the posting is gone when the board token
                # came explicitly from a greenhouse.io URL. For host-derived
                # guesses (embedded boards on custom domains) a 404 usually
                # means the guess was wrong, not that the job is dead.
                if early.status is Status.GONE and cand != board:
                    last = AtsResult(Status.ERROR, source=self.name)
                else:
                    last = early
                continue
            assert resp is not None
            return self.result(greenhouse_text(resp.json()))
        return last


class Lever(AtsResolver):
    name = "lever"
    markers = ("lever.co",)

    def canonical(self, url: str) -> str | None:
        match = re.search(r"(jobs(?:\.eu)?\.lever\.co)/([^/?]+)/([0-9a-f-]{36})", url)
        return (
            f"https://{match.group(1).lower()}/{match.group(2)}/{match.group(3)}" if match else None
        )

    def fetch(self, url: str) -> AtsResult:
        match = re.search(r"lever\.co/([^/]+)/([0-9a-f-]{36})", url)
        if not match:
            return UNSUPPORTED
        api_host = "api.eu.lever.co" if ".eu.lever.co" in url else "api.lever.co"
        resp = self.get(f"https://{api_host}/v0/postings/{match.group(1)}/{match.group(2)}")
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        return self.result(lever_text(resp.json()))


class Ashby(AtsResolver):
    name = "ashby"
    markers = ("ashbyhq.com",)

    def canonical(self, url: str) -> str | None:
        match = re.search(r"ashbyhq\.com/([^/?]+)/([0-9a-f-]{36})", url)
        return f"https://jobs.ashbyhq.com/{match.group(1)}/{match.group(2)}" if match else None

    def fetch(self, url: str) -> AtsResult:
        match = re.search(r"ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
        if not match:
            return UNSUPPORTED
        org, job_id = unquote(match.group(1)), match.group(2)
        resp = self.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        for job in resp.json().get("jobs", []):
            if job.get("id") == job_id or job_id in str(job.get("jobUrl", "")):
                return self.result(ashby_text(job))
        return AtsResult(Status.GONE, source=self.name)


class SmartRecruiters(AtsResolver):
    name = "smartrecruiters"
    markers = ("smartrecruiters.com",)

    _SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")

    def canonical(self, url: str) -> str | None:
        match = re.search(r"smartrecruiters\.com/([^/?]+)/(\d+)", url)
        return (
            f"https://jobs.smartrecruiters.com/{match.group(1)}/{match.group(2)}" if match else None
        )

    def fetch(self, url: str) -> AtsResult:
        match = re.search(r"smartrecruiters\.com/([^/]+)/(\d+)", url)
        if not match:
            return UNSUPPORTED
        resp = self.get(
            f"https://api.smartrecruiters.com/v1/companies/{match.group(1)}/postings/{match.group(2)}"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        data = resp.json()
        # A removed public ad can retain its complete description at HTTP 200.
        # Missing fields are not closure evidence, and truthy strings are not
        # booleans. INTERNAL means unavailable to this public-board audience.
        if data.get("active") is False or data.get("visibility") == "INTERNAL":
            return AtsResult(Status.GONE, source=self.name)
        loc = data.get("location") or {}
        sections = (data.get("jobAd") or {}).get("sections") or {}
        body = [clean_html((sections.get(k) or {}).get("text", "")) for k in self._SECTIONS]
        return self.result(
            join(
                data.get("name"),
                " ".join(str(loc.get(k, "")) for k in ("city", "region", "country")),
                *body,
            )
        )


class Workday(AtsResolver):
    name = "workday"
    markers = ("myworkdayjobs.com",)

    def canonical(self, url: str) -> str | None:
        parsed = urlparse(url)
        path = re.sub(r"^/[a-z]{2}-[a-z]{2}/", "/", parsed.path, flags=re.IGNORECASE)
        match = re.match(r"^/([^/]+)/job/(.+?)/?$", path)
        return (
            f"https://{parsed.netloc.lower()}/{match.group(1)}/job/{match.group(2)}"
            if match
            else None
        )

    def fetch(self, url: str) -> AtsResult:
        parsed = urlparse(url)
        tenant = parsed.netloc.split(".")[0]
        path = re.sub(r"^/[a-z]{2}-[a-z]{2}/", "/", parsed.path, flags=re.IGNORECASE)
        match = re.match(r"^/([^/]+)/job/(.+)$", path)
        if not match:
            return UNSUPPORTED
        resp = self.get(
            f"https://{parsed.netloc}/wday/cxs/{tenant}/{match.group(1)}/job/{match.group(2)}"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        info = resp.json().get("jobPostingInfo") or {}
        result = self.result(
            join(
                info.get("title"),
                info.get("location"),
                info.get("startDate"),
                clean_html(info.get("jobDescription", "")),
            )
        )
        return replace(result, posted=_iso_date(info.get("startDate")))


class Oracle(AtsResolver):
    """Oracle Recruiting (Fusion HCM) candidate experience: the posting page is
    rendered by script, but the same tenant serves the requisition as JSON."""

    name = "oracle"
    markers = ("oraclecloud.com",)
    # Requisition ids are usually numeric; some tenants prefix them (W737248).
    _JOB = re.compile(r"/sites/([^/]+)/job/([A-Za-z0-9_-]+)")

    def canonical(self, url: str) -> str | None:
        parsed = urlparse(url)
        match = self._JOB.search(parsed.path)
        return (
            f"https://{parsed.netloc.lower()}/hcmUI/CandidateExperience/en/sites/"
            f"{match.group(1)}/job/{match.group(2)}"
            if match
            else None
        )

    def fetch(self, url: str) -> AtsResult:
        parsed = urlparse(url)
        match = self._JOB.search(parsed.path)
        if not match:
            return UNSUPPORTED
        site, job_id = match.groups()
        resp = self.get(
            f"https://{parsed.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?onlyData=true&expand=all&finder=ById;siteNumber={site},Id=%22{job_id}%22"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        items = resp.json().get("items") or []
        # A requisition that is no longer posted comes back as an empty list,
        # not a 404.
        if not items:
            return AtsResult(Status.GONE, source=self.name)
        return replace(
            self.result(oracle_text(items[0])),
            posted=_iso_date(items[0].get("ExternalPostedStartDate") or items[0].get("PostedDate")),
        )


class Workable(AtsResolver):
    name = "workable"
    markers = ("workable.com",)
    _JOB = re.compile(r"workable\.com/([^/]+)/j/([A-Za-z0-9]+)")

    def canonical(self, url: str) -> str | None:
        match = self._JOB.search(url)
        return (
            f"https://apply.workable.com/{match.group(1)}/j/{match.group(2).upper()}/"
            if match
            else None
        )

    def fetch(self, url: str) -> AtsResult:
        match = self._JOB.search(url)
        if not match:
            return UNSUPPORTED
        resp = self.get(
            f"https://apply.workable.com/api/v2/accounts/{match.group(1)}/jobs/{match.group(2)}"
        )
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        data = resp.json()
        return replace(self.result(workable_text(data)), posted=_iso_date(data.get("published")))


class ICims(AtsResolver):
    """An iCIMS portal posting, read from the frame its public page embeds.

    The public URL serves the portal's chrome with the posting inside an
    iframe, which neither the static fetch nor the browser's body text reads:
    GDMS answered 7,417 characters of site navigation and none of the posting,
    Joby 472 (2026-10-05). The frame itself, the same URL with in_iframe=1,
    carries the posting as schema.org JobPosting JSON-LD. A posting that is
    not public answers 410: 40 of 40 ids missing from two tenants' sitemaps
    did, and 6 of 6 listed ids answered 200 with the JSON-LD.
    """

    name = "icims"
    markers = ("icims.com",)
    _JOB = re.compile(r"/jobs/(\d+)(?:/[^/]*)?/job")
    _LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)

    def canonical(self, url: str) -> str | None:
        parsed = urlparse(url)
        match = self._JOB.search(parsed.path)
        return f"https://{parsed.netloc.lower()}/jobs/{match.group(1)}/job" if match else None

    def fetch(self, url: str) -> AtsResult:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        match = self._JOB.search(parsed.path)
        if not host.endswith(".icims.com") or not match:
            return UNSUPPORTED
        resp = self.get(f"https://{host}/jobs/{match.group(1)}/job?in_iframe=1")
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        posting = next(
            (d for d in map(_json_or_none, self._LD.findall(resp.text)) if _is_job_posting(d)),
            None,
        )
        if posting is None:
            return self.result(None)
        places = posting.get("jobLocation") or []
        places = places if isinstance(places, list) else [places]
        result = self.result(
            join(
                posting.get("title"),
                "; ".join(_ld_place(p) for p in places if _ld_place(p)),
                clean_html(posting.get("description") or ""),
            )
        )
        return replace(result, posted=_iso_date(posting.get("datePosted")))


class Apple(AtsResolver):
    """A jobs.apple.com posting, read from the careers site's own details API.

    The posting page builds its text from JSON embedded in the page, so the
    static fetch extracts the site's navigation alone (3,278 characters, none
    of the posting, on 2026-10-05) and that clears static_fetch_min_chars.
    jobDetails takes the id the page URL carries.

    A requisition that is no longer posted answers 404: 68 of 68 requisitions
    from an aggregator's history that the board no longer listed did, and 66
    of 66 it still listed answered 200. A 200 is not proof the posting is
    listed: one requisition missing from the board answered 200 with its
    full text, so the board pull, not this, closes that one. An unknown
    location suffix answers the requisition itself, not a 404.
    """

    name = "apple"
    # The slash keeps the marker out of the mail domains, which inherit
    # resolver markers: whether jobs.apple.com sends application mail has
    # not been measured.
    markers = ("jobs.apple.com/",)
    # A pipeline row's id is "PIPE-<n>" in the search API, and its page and
    # jobDetails both take <n>; both also accept the prefixed form.
    _JOB = re.compile(
        r"jobs\.apple\.com/[a-z]{2}-[a-z]{2}/details/(?:PIPE-)?([0-9][0-9-]*)(/[^/?#]+)?"
    )
    _SECTIONS = (
        ("jobSummary", None),
        ("description", "Description"),
        ("responsibilities", "Responsibilities"),
        ("minimumQualifications", "Minimum Qualifications"),
        ("preferredQualifications", "Preferred Qualifications"),
    )

    def canonical(self, url: str) -> str | None:
        # The listing fetcher writes /en-us/details/{id}/{slug}; any locale,
        # query or trailing page (/locationPicker) of the same posting is it.
        match = self._JOB.search(url)
        if not match:
            return None
        return f"https://jobs.apple.com/en-us/details/{match.group(1)}{match.group(2) or ''}"

    def fetch(self, url: str) -> AtsResult:
        match = self._JOB.search(url)
        if not match:
            return UNSUPPORTED
        job_id = match.group(1)
        resp = self.get(f"https://jobs.apple.com/api/v1/jobDetails/{job_id}")
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        data = resp.json().get("res") or {}
        # For an unknown location suffix jobNumber is the bare requisition and
        # selectedLocation is one of its other places, so name them all.
        place = data.get("selectedLocation") if data.get("jobNumber") == job_id else None
        places = [place] if place else data.get("locations") or []
        result = self.result(
            join(
                data.get("postingTitle"),
                "; ".join(
                    ", ".join(dict.fromkeys(x for x in (p.get("name"), p.get("countryName")) if x))
                    for p in places
                ),
                *(
                    join(label, data.get(key)) if data.get(key) else None
                    for key, label in self._SECTIONS
                ),
            )
        )
        # A managed pipeline role's date is the time of the request, here as
        # in the search API.
        if data.get("managedPipelineRole"):
            return result
        return replace(result, posted=_iso_date(data.get("postDateInGMT")))


def _json_or_none(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _is_job_posting(data: object) -> TypeGuard[dict]:
    return isinstance(data, dict) and data.get("@type") == "JobPosting"


def _ld_place(place: object) -> str:
    address = place.get("address") if isinstance(place, dict) else None
    if not isinstance(address, dict):
        return ""
    # iCIMS writes UNAVAILABLE where a tenant left a part empty (AMD Hsinchu
    # has no region, Peraton's remote postings no city).
    keys = ("addressLocality", "addressRegion", "addressCountry")
    parts = (str(address.get(k) or "").strip() for k in keys)
    return ", ".join(p for p in parts if p and p != "UNAVAILABLE")


RESOLVERS: list[AtsResolver] = [
    Greenhouse(),
    Lever(),
    Ashby(),
    SmartRecruiters(),
    Workday(),
    Oracle(),
    Workable(),
    ICims(),
    Apple(),
]


# Domains an applicant-tracking system sends MAIL from. This is NOT the same
# question as which postings we can resolve, and the two lists cannot be
# derived from each other in either direction.
#
# Postings differ from mail: Greenhouse posts on greenhouse.io and mails from
# greenhouse-mail.io; Workday posts on myworkdayjobs.com and mails from
# myworkday.com. Those two are the largest sources of applications in the
# corpus, so a rule that assumed the pair would silently miss most of it.
#
# And most entries below have NO resolver at all - we cannot read their
# postings and they are still applicant-tracking systems when they send mail.
#
# MEMBERSHIP IS EARNED, not assumed from a brand being well known. Each domain
# here has at least 8 messages in the corpus of which at least 75% are
# application LIFECYCLE mail - acknowledgement, rejection, info_request,
# assessment, interview, offer, closure - rather than outreach or marketing.
# The measured share is recorded beside each one so the next reader can
# re-check it rather than trust it.
#
# Four job boards were proposed for this list and are deliberately absent,
# because their mail is mostly marketing rather than news about an application
# you sent: untapped.io (20% lifecycle over 217 messages), ripplematch.com
# (21% over 186), hi.wellfound.com (62% over 85) and codesignal.com (71% over
# 56). Adding them would mark their marketing as near-proof of a real
# application, which is exactly the failure #211 removed for RippleMatch.
# app.bamboohr.com is 100% lifecycle but over only 4 messages, so it waits for
# evidence rather than joining on reputation.
_MAIL_ONLY_DOMAINS = (
    # Mail domain differs from the posting domain of a provider we do resolve.
    "greenhouse-mail.io",
    "myworkday.com",
    # Applicant-tracking systems with no resolver: we cannot read their
    # postings, and their mail is still about an application you sent.
    "successfactors.com",
    "jobvite.com",  # 100% lifecycle, n=35
    "candidates.workablemail.com",  # 100%, n=17
    "ats.rippling.com",  # 100%, n=31
    "appreview.gem.com",  # 100%, n=8
    "applytojob.com",  # 100%, n=10
    "workflow.mail.us2.cloud.oracle.com",  # 100%, n=38
    "welcometothejungle.com",  # 93%, n=14
    # An assessment platform, included on the same evidence: an invitation to
    # a coding assessment is sent because an application exists.
    "hackerrankforwork.com",  # 92%, n=78
)


def is_ats_email_domain(domain: str | None) -> bool:
    """Did this mail come from an applicant-tracking system?

    Near-proof that an application is real: across 1,779 mail-derived
    applications, 98.9% of those whose first message came from an ATS domain
    were genuine, against 85.2% of those that did not.

    Its ABSENCE proves nothing, which is the important half. 46% of genuine
    applications are not ATS-sent, because plenty of employers mail from their
    own domain - Epic Games, MathWorks, Lockheed Martin, Morgan Stanley and
    Citadel all do. Gating on this would discard 884 applications to remove 131
    bad ones, 5.7 real losses per junk removal. Rank on it; never filter on it.
    """
    d = (domain or "").lower().strip().rstrip(".")
    if not d:
        return False
    known = [m for r in RESOLVERS for m in r.markers if "." in m]
    known.extend(_MAIL_ONLY_DOMAINS)
    return any(d == k or d.endswith("." + k) for k in known)


def canonicalize(url: str) -> str | None:
    """Canonical clickable URL for a posting."""
    for resolver in RESOLVERS:
        if not resolver.matches(url):
            continue
        try:
            return resolver.canonical(url)
        except Exception as exc:
            logger.debug(f"[{resolver.name}] canonicalization error {url}: {exc}")
            return None
    return None


def resolve(url: str) -> AtsResult:
    for resolver in RESOLVERS:
        if not resolver.matches(url):
            continue
        try:
            result = resolver.fetch(url)
        except Exception as exc:
            logger.debug(f"[{resolver.name}] resolver error {url}: {exc}")
            return AtsResult(Status.ERROR, source=resolver.name)
        if result.ok:
            logger.info(f"ATS hit [{resolver.name}]: {len(result.text or '')} chars from {url}")
        elif result.status is Status.GONE:
            logger.info(f"ATS reports posting gone [{resolver.name}]: {url}")
        return result
    return UNSUPPORTED
