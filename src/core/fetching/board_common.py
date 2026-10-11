"""Shared request and posting boundaries for board pulls."""

from __future__ import annotations

import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import ftfy
import requests

from core.fetching.client import pace
from core.fetching.client import session as _session
from core.fetching.posting import JobPosting
from core.fetching.urls import normalize_url


# Every request in a pull waits out its host floor, including paginated calls.
def _get(url: str, **kwargs: Any) -> requests.Response:
    pace(url)
    return _session.get(url, **kwargs)


def _post(url: str, **kwargs: Any) -> requests.Response:
    pace(url)
    return _session.post(url, **kwargs)


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


def posting_from_listing(
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


def with_query_parameters(url: str, **params: str) -> str:
    """The listings URL with these query parameters added, existing ones kept."""
    parsed = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()} | params
    return urlunparse(parsed._replace(query=urlencode(query)))


class PartialPull(Exception):
    """A pull that could not prove it saw every open posting.

    Ingest admits what it holds and retires nothing by absence, because a
    posting outside what was seen is not evidence of a closure. Closures on
    such a board come from re-verification instead."""

    def __init__(self, postings: list[JobPosting]):
        super().__init__(f"{len(postings)} postings from an incomplete pull")
        self.postings = postings


def iso_posted_timestamp(value: str | None) -> int:
    if not value:
        return 0
    return int(datetime.datetime.fromisoformat(value).timestamp())


def location_label(loc: dict) -> str:
    """City, region, country as the ATS spells them, skipping what is unset."""
    return ", ".join(str(loc[k]) for k in ("city", "region", "country") if loc.get(k))
