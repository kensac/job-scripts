"""Shared ATS result, request, and posting-text behavior."""

from __future__ import annotations

import datetime
import enum
import html
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import ftfy
import requests
from bs4 import BeautifulSoup

from core.fetching.client import session as _session

logger = logging.getLogger(__name__)


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


def parse_posted_date(text: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def join_posting_sections(*parts: str | None) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip()).strip()


# The text of one posting from the JSON its board's API returns. One function
# per format, shared by the resolver (which fetches a posting by URL) and the
# listing fetchers in core/fetching/boards/ (which get the same JSON for every
# posting in one call), so the text a listing stores is exactly the text a
# resolver would have fetched for it, and the per-posting fetch is not needed.


def greenhouse_text(data: dict) -> str:
    return join_posting_sections(
        data.get("title"),
        (data.get("location") or {}).get("name"),
        clean_html(data.get("content", "")),
    )


def lever_text(data: dict) -> str:
    lists = [
        join_posting_sections(s.get("text"), clean_html(s.get("content", "")))
        for s in data.get("lists", [])
    ]
    return join_posting_sections(
        data.get("text"),
        (data.get("categories") or {}).get("location"),
        clean_html(data.get("description", "")),
        *lists,
        clean_html(data.get("additional", "")),
    )


def ashby_text(job: dict) -> str:
    comp = (job.get("compensation") or {}).get("compensationTierSummary")
    return join_posting_sections(
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
    return join_posting_sections(
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
    return join_posting_sections(
        item.get("Title"),
        item.get("PrimaryLocation"),
        clean_html(item.get("ExternalDescriptionStr") or ""),
        clean_html(item.get("ExternalResponsibilitiesStr") or ""),
        clean_html(item.get("ExternalQualificationsStr") or ""),
    )


def workable_text(data: dict) -> str:
    loc = data.get("location") or {}
    return join_posting_sections(
        data.get("title"),
        ", ".join(str(loc[k]) for k in ("city", "region", "country") if loc.get(k)),
        clean_html(data.get("description") or ""),
        clean_html(data.get("requirements") or ""),
        clean_html(data.get("benefits") or ""),
    )


def rippling_text(data: dict) -> str:
    """One posting as Rippling's detail call returns it: the employer's
    paragraph, then the role's."""
    description = data.get("description") or {}
    return join_posting_sections(
        data.get("name"),
        "; ".join(data.get("workLocations") or []),
        clean_html(description.get("company") or ""),
        clean_html(description.get("role") or ""),
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

    def get(self, url: str, **kwargs: Any) -> requests.Response | None:
        return self._send(_session.get, url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response | None:
        return self._send(_session.post, url, **kwargs)

    def _send(
        self, send: Callable[..., requests.Response], url: str, **kwargs: Any
    ) -> requests.Response | None:
        """The answer, or None when there was none to read: a resolver
        reads None as ERROR, never as the posting's closure."""
        try:
            return send(url, **kwargs)
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
