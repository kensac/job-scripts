"""Resolve Workday posting detail and availability."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

import requests

from core.fetching.ats.common import (
    UNSUPPORTED,
    AtsResolver,
    AtsResult,
    Status,
    clean_html,
    join_posting_sections,
    parse_posted_date,
)
from core.fetching.client import stdlib_session


class Workday(AtsResolver):
    name = "workday"
    markers = ("myworkdayjobs.com",)

    def get(self, url: str, **kwargs: Any) -> requests.Response | None:
        # Through the standard library, as the listing pulls are: Workday's
        # Cloudflare refuses the requests session (client.stdlib_session).
        return self._send(stdlib_session.get, url, **kwargs)

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
        if _unpublished(resp):
            return AtsResult(Status.GONE, source=self.name)
        early = self.from_response(resp)
        if early is not None:
            return early
        assert resp is not None
        info = resp.json().get("jobPostingInfo") or {}
        result = self.result(
            join_posting_sections(
                info.get("title"),
                info.get("location"),
                info.get("startDate"),
                clean_html(info.get("jobDescription", "")),
            )
        )
        return replace(result, posted=parse_posted_date(info.get("startDate")))


# Workday's own answer for a posting the tenant took down: 403 with a JSON
# body naming error S22, "permission denied". An id that never existed is 404
# S21, which `from_response` already reads as gone. Read on 2026-11-25 from
# advanceauto: 661 postings answered S22 and were recorded as fetches that
# returned nothing, so none was ever closed, while its live postings answered
# 200. A block is Cloudflare's HTML challenge, never this body, so a refused
# address is not read as a closure.
UNPUBLISHED = "S22"


def _unpublished(resp: requests.Response | None) -> bool:
    if resp is None or resp.status_code != 403:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("errorCode") == UNPUBLISHED
