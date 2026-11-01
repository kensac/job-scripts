"""Resolve ADP Workforce Now posting detail and availability."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode

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
from core.fetching.client import session

ADP_HOSTS = ("workforcenow.adp.com", "workforcenow.cloud.adp.com")
ADP_REQUISITIONS = "/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
# The career center page a posting opens on; jobId is the ExternalJobID.
ADP_POSTING = "/mascsr/default/mdf/recruitment/recruitment.html"
# The host is a pool of nodes behind a load balancer, and some nodes cannot
# serve some tenants: they answer a bare 404 "default backend - 404" or a 500.
# Without cookies a request lands on a node at random, so Prosperity Bank was
# refused 11 of 25 times while six other tenants answered 60 of 60
# (2026-11-01). The balancer pins a client to a node by cookie, and a pull
# process whose shared session had been pinned got a 500 on every ADP tenant
# while a fresh process read the same tenants. So every request goes out
# without the host's cookies, and a node's refusal is sent again, to another
# node, up to this many times. A 503 is the host shedding load and is not
# retried; a cid no tenant has answers an openresty 404 page, also final.
_ADP_ATTEMPTS = 8


def _node_refused(resp: requests.Response) -> bool:
    return resp.status_code == 500 or (
        resp.status_code == 404 and resp.text.startswith("default backend")
    )


def adp_get(
    get: Callable[..., requests.Response | None], url: str, **kwargs: Any
) -> requests.Response | None:
    """An answer from a node that serves the tenant, or the last refusal."""
    resp = None
    for _ in range(_ADP_ATTEMPTS):
        for cookie in [c for c in session.cookies if c.domain.lstrip(".").endswith("adp.com")]:
            session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        resp = get(url, **kwargs)
        if resp is None or not _node_refused(resp):
            return resp
    return resp


def adp_posting_url(host: str, cid: str, job_id: str) -> str:
    return f"https://{host}{ADP_POSTING}?{urlencode({'cid': cid, 'jobId': job_id})}"


class Adp(AtsResolver):
    """A posting opens on a career center page that renders in the browser,
    so its text is read from the detail call the page makes.

    The detail call takes the jobId the page does. A posting the board no
    longer lists answers 200 with an empty requisition, no `itemID` and no
    title: of 33 catalog postings on 2026-11-01, the 19 their boards listed
    answered their text and the 14 they did not answered that. Any 404 is
    the tenant or a node, never the posting, so it is an error.
    """

    name = "adp"
    markers = ADP_HOSTS
    _HOST = re.compile(r"//(workforcenow(?:\.cloud)?\.adp\.com)" + re.escape(ADP_POSTING))
    _CID = re.compile(r"[?&]cid=([0-9a-fA-F-]{36})")
    _JOB = re.compile(r"[?&]jobId=(\d+)")

    def _parts(self, url: str) -> tuple[str, str, str] | None:
        host, cid, job = self._HOST.search(url), self._CID.search(url), self._JOB.search(url)
        if not (host and cid and job):
            return None
        return host.group(1), cid.group(1).lower(), job.group(1)

    def canonical(self, url: str) -> str | None:
        parts = self._parts(url)
        return adp_posting_url(*parts) if parts else None

    def fetch(self, url: str) -> AtsResult:
        parts = self._parts(url)
        if not parts:
            return UNSUPPORTED
        host, cid, job = parts
        resp = adp_get(self.get, f"https://{host}{ADP_REQUISITIONS}/{job}", params={"cid": cid})
        if resp is None or resp.status_code != 200:
            return AtsResult(Status.ERROR, source=self.name)
        data = resp.json()
        if not data.get("itemID"):
            return AtsResult(Status.GONE, source=self.name)
        text = join_posting_sections(
            data.get("requisitionTitle"), clean_html(data.get("requisitionDescription") or "")
        )
        return replace(self.result(text), posted=parse_posted_date(data.get("postDate")))
