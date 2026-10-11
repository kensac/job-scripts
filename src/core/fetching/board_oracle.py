"""Fetch listings from the Oracle board format."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse, urlunparse

from core.fetching.board_common import (
    PartialPull,
    _get,
    iso_posted_timestamp,
    posting_from_listing,
)
from core.fetching.posting import JobPosting

# Oracle Recruiting's candidate-experience API honoured limit=200 on Nokia's
# site (617 postings) on 2026-09-05.
_ORACLE_PAGE = 200


def _oracle(url: str, company: str) -> list[JobPosting]:
    """GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?siteNumber=CX_1

    The site number on the listings URL is the one in the tenant's own posting
    URLs (/hcmUI/CandidateExperience/en/sites/{site}/job/{id}). The finder
    goes on the URL verbatim because Oracle reads its ; and , unencoded. A row
    carries a short description and, when the tenant filled them in, its
    qualifications and responsibilities; the full text is one more call per
    row, which the resolver makes.
    """
    parsed = urlparse(url)
    site = (parse_qs(parsed.query).get("siteNumber") or ["CX_1"])[0]
    endpoint = urlunparse(parsed._replace(query="", fragment=""))
    base = f"https://{parsed.netloc}/hcmUI/CandidateExperience/en/sites/{site}/job/"
    out: list[JobPosting] = []
    offset = 0
    total: int | None = None
    while True:
        finder = (
            f"findReqs;siteNumber={site},limit={_ORACLE_PAGE},offset={offset},"
            "sortBy=POSTING_DATES_DESC"
        )
        resp = _get(
            f"{endpoint}?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}",
        )
        resp.raise_for_status()
        items = resp.json().get("items") or [{}]
        page = items[0].get("requisitionList") or []
        for j in page:
            p = posting_from_listing(
                company,
                j.get("Title"),
                [
                    j.get("PrimaryLocation"),
                    *[s.get("Name") for s in j.get("secondaryLocations") or []],
                ],
                base + str(j["Id"]) if j.get("Id") else None,
                iso_posted_timestamp(f"{j['PostedDate']}T00:00:00+00:00")
                if j.get("PostedDate")
                else 0,
                raw=j,
            )
            if p:
                out.append(p)
        if total is None:
            total = int(items[0].get("TotalJobsCount") or 0)
        offset += len(page)
        if not page or offset >= total:
            # Oracle serves at most 10,000 rows of a search: past that the
            # page is empty and the count reads 0 (AutoZone listed 10,788,
            # Marriott 12,979, on 2026-10-05). The newest 10,000 arrive, by
            # the sort above, and the rest are unseen, not closed.
            if offset < total:
                raise PartialPull(out)
            return out
