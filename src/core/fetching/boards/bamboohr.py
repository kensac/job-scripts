"""Fetch listings from the BambooHR board format."""

from __future__ import annotations

from urllib.parse import urlparse

from core.fetching.boards.common import PartialPull, _get, location_label, posting_from_listing
from core.fetching.posting import JobPosting


def _bamboohr(url: str, company: str) -> list[JobPosting]:
    """GET https://{tenant}.bamboohr.com/careers/list

    One JSON reply of every open posting and `meta.totalCount`, with no
    paging, text or date. A tenant that does not exist redirects to an HTML
    page, which fails as JSON. The posting page is a script shell, so its
    text comes from the browser tier.
    """
    resp = _get(url, allow_redirects=False)
    resp.raise_for_status()
    if resp.status_code != 200:
        raise ValueError(f"BambooHR answered {resp.status_code} for {url}")
    data = resp.json()
    rows = data["result"]
    origin = f"https://{urlparse(url).netloc}"
    out: list[JobPosting] = []
    for j in rows:
        loc = j.get("location") or {}
        ats = j.get("atsLocation") or {}
        place = location_label(
            {
                "city": loc.get("city") or ats.get("city"),
                "region": loc.get("state") or ats.get("state") or ats.get("province"),
                "country": ats.get("country"),
            }
        )
        p = posting_from_listing(
            company,
            j.get("jobOpeningName"),
            [place],
            f"{origin}/careers/{j['id']}" if j.get("id") else None,
            0,
            raw=j,
        )
        if p:
            out.append(p)
    if len(rows) < int(data["meta"]["totalCount"]):
        raise PartialPull(out)
    return out
