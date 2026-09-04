from __future__ import annotations

import datetime
import enum
import html
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import ClassVar
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


class AtsResolver(ABC):
    name: ClassVar[str]
    markers: ClassVar[tuple[str, ...]]
    enabled: ClassVar[bool] = True

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


class ICims(AtsResolver):
    """No public content API; registered for URL canonicalization only."""

    name = "icims"
    markers = ("icims.com",)
    enabled = False

    def canonical(self, url: str) -> str | None:
        parsed = urlparse(url)
        match = re.search(r"/jobs/(\d+)(?:/[^/]*)?/job", parsed.path)
        return f"https://{parsed.netloc.lower()}/jobs/{match.group(1)}/job" if match else None

    def fetch(self, url: str) -> AtsResult:
        return UNSUPPORTED


RESOLVERS: list[AtsResolver] = [
    Greenhouse(),
    Lever(),
    Ashby(),
    SmartRecruiters(),
    Workday(),
    ICims(),
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
    """Canonical clickable URL for a posting, independent of whether the
    provider's content bypass is enabled."""
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
        if not resolver.enabled or not resolver.matches(url):
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
