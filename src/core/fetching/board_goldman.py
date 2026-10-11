"""Fetch listings from the Goldman board format."""

from __future__ import annotations

from core.fetching.ats import (
    goldman_place,
    goldman_text,
)
from core.fetching.board_common import (
    PartialPull,
    _post,
    iso_posted_timestamp,
    posting_from_listing,
)
from core.fetching.posting import JobPosting

# Goldman's roleSearch refuses a pageSize above 250 ("must be less than or
# equal to 250", 2026-10-05).
_GOLDMAN_PAGE = 250

# The experiences a person outside the firm can apply to. The schema's fourth,
# INTERNAL_MOBILITY, is for employees. The careers site's results page asks
# for the first two (715 roles on 2026-10-05) and its students page for
# CAMPUS (238), which is where the internships and analyst programs are.
_GOLDMAN_EXPERIENCES = ["EARLY_CAREER", "PROFESSIONAL", "CAMPUS"]

_GOLDMAN_QUERY = """query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items {
      roleId jobTitle corporateTitle jobFunction division status lastPostedDate
      locations { primary city state country }
      compensation { minSalary maxSalary currency }
      descriptionHtml
      externalSource { sourceId }
    }
  }
}"""


def _goldman(url: str, company: str) -> list[JobPosting]:
    """POST https://api-higher.gs.com/gateway/api/v1/graphql, the roleSearch
    behind higher.gs.com, unauthenticated.

    Pages are numbered from 0. A refused request is HTTP 200 with `errors`
    and no data, which must fail the pull rather than read as an empty board.
    The role's public page is /roles/{externalSource.sourceId}; the roleId
    carries a suffix (_GS_MID_CAREER, _GS_CAMPUS, or a uuid) the page does not.
    """
    out: list[JobPosting] = []
    seen: set[str] = set()
    total: int | None = None
    number = 0
    while True:
        resp = _post(
            url,
            json={
                "operationName": "GetRoles",
                "query": _GOLDMAN_QUERY,
                "variables": {
                    "searchQueryInput": {
                        "page": {"pageSize": _GOLDMAN_PAGE, "pageNumber": number},
                        "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"},
                        "filters": [],
                        "experiences": _GOLDMAN_EXPERIENCES,
                        "searchTerm": "",
                    }
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"roleSearch refused: {data['errors']}")
        result = data["data"]["roleSearch"]
        page = result.get("items") or []
        if total is None:
            total = int(result.get("totalCount") or 0)
        for j in page:
            source_id = (j.get("externalSource") or {}).get("sourceId")
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            p = posting_from_listing(
                company,
                j.get("jobTitle"),
                [goldman_place(loc) for loc in j.get("locations") or []],
                f"https://higher.gs.com/roles/{source_id}",
                iso_posted_timestamp(j.get("lastPostedDate")),
                raw=j,
                description=goldman_text(j) if j.get("descriptionHtml") else "",
            )
            if p:
                out.append(p)
        number += 1
        if len(page) < _GOLDMAN_PAGE:
            break
    # Newest first, so a role posted mid-pull pushes rows onto later pages
    # and is read twice rather than skipped; a role closed mid-pull pulls one
    # past the page boundary unseen, and the count catches it.
    if len(seen) < total:
        raise PartialPull(out)
    return out
