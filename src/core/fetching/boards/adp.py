"""Fetch listings from the ADP Workforce Now board format."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from core.fetching.ats.adp import adp_get, adp_posting_url
from core.fetching.boards.common import (
    PartialPull,
    _get,
    iso_posted_timestamp,
    posting_from_listing,
    with_query_parameters,
)
from core.fetching.posting import JobPosting

# The most rows a page returns: $top above 20 still answers 20 (2026-11-01).
_ADP_PAGE = 20


def adp_external_id(row: dict) -> str | None:
    """The id the career center page and the detail call take as jobId."""
    fields = (row.get("customFieldGroup") or {}).get("stringFields") or []
    return next(
        (
            f.get("stringValue")
            for f in fields
            if (f.get("nameCode") or {}).get("codeValue") == "ExternalJobID"
        ),
        None,
    )


def _adp_location(loc: dict) -> str:
    address = loc.get("address") or {}
    region = (address.get("countrySubdivisionLevel1") or {}).get("codeValue")
    place = ", ".join(p for p in (address.get("cityName"), region) if p)
    return place or (loc.get("nameCode") or {}).get("shortName") or ""


def _adp(url: str, company: str) -> list[JobPosting]:
    """GET https://workforcenow.adp.com/mascsr/default/careercenter/public/
    events/staffing/v1/job-requisitions?cid={cid}

    `$skip` is one-based: `$skip=0` answers the first page short by one row.
    Every page states `meta.totalNumber`, and a page past the end is empty
    (PathStone, 30 postings, 2026-11-01). The tenant is `cid` alone; the
    career center page's `ccId`, `lang` and `type` change nothing in the
    answer. Rows carry no text; the resolver reads it from the detail call.
    """
    parsed = urlparse(url)
    cid = parse_qs(parsed.query)["cid"][0]
    seen: dict[str, JobPosting] = {}
    stated = 0
    skip = 1
    while True:
        page_url = with_query_parameters(url, **{"$top": str(_ADP_PAGE), "$skip": str(skip)})
        resp = adp_get(_get, page_url)
        assert resp is not None
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("jobRequisitions") or []
        stated = max(stated, int(data["meta"]["totalNumber"]))
        for row in rows:
            job_id = adp_external_id(row)
            p = posting_from_listing(
                company,
                row.get("requisitionTitle"),
                [_adp_location(loc) for loc in row.get("requisitionLocations") or []],
                adp_posting_url(parsed.netloc, cid, job_id) if job_id else None,
                iso_posted_timestamp(row.get("postDate")),
                raw=row,
            )
            if p:
                seen.setdefault(p.url, p)
        skip += len(rows)
        if not rows or skip > stated:
            break
    out = list(seen.values())
    if len(out) < stated:
        raise PartialPull(out)
    return out
