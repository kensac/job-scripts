"""Resolve Icims posting detail and availability."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TypeGuard
from urllib.parse import urlparse

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    clean_html,
    join_posting_sections,
    parse_posted_date,
)


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
            (
                d
                for d in map(parse_json_ld, self._LD.findall(resp.text))
                if is_job_posting_json_ld(d)
            ),
            None,
        )
        if posting is None:
            return self.result(None)
        places = posting.get("jobLocation") or []
        places = places if isinstance(places, list) else [places]
        result = self.result(
            join_posting_sections(
                posting.get("title"),
                "; ".join(json_ld_location(p) for p in places if json_ld_location(p)),
                clean_html(posting.get("description") or ""),
            )
        )
        return replace(result, posted=parse_posted_date(posting.get("datePosted")))


def parse_json_ld(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None


def is_job_posting_json_ld(data: object) -> TypeGuard[dict]:
    return isinstance(data, dict) and data.get("@type") == "JobPosting"


def json_ld_location(place: object) -> str:
    address = place.get("address") if isinstance(place, dict) else None
    if not isinstance(address, dict):
        return ""
    # iCIMS writes UNAVAILABLE where a tenant left a part empty (AMD Hsinchu
    # has no region, Peraton's remote postings no city).
    keys = ("addressLocality", "addressRegion", "addressCountry")
    parts = (str(address.get(k) or "").strip() for k in keys)
    return ", ".join(p for p in parts if p and p != "UNAVAILABLE")
