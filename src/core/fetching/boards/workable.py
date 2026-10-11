"""Fetch listings from the Workable board format."""

from __future__ import annotations

from urllib.parse import urlparse

from core.fetching.boards.common import (
    _post,
    iso_posted_timestamp,
    location_label,
    posting_from_listing,
)
from core.fetching.posting import JobPosting


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
        resp = _post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("results") or []
        for j in page:
            p = posting_from_listing(
                company,
                j.get("title"),
                [location_label(j.get("location") or {})],
                f"https://apply.workable.com/{account}/j/{j['shortcode']}/"
                if j.get("shortcode")
                else None,
                iso_posted_timestamp(j.get("published")),
                raw=j,
            )
            if p:
                out.append(p)
        token = data.get("nextPage")
        if not page or not token:
            return out
        body = {**body, "token": token}
