"""Per-board analytics: what each source supplies, and what it is worth.

Every rate here ships with the numerator and denominator that produced it, and
returns a null value rather than a number when the denominator is too small to
mean anything. Boards in this catalog span 18,876 postings down to 1, so an
un-floored rate would let the smallest board render the loudest percentage.

Two shape caveats are baked into the response rather than left for the reader
to rediscover, because both invert the obvious reading of the numbers:

`active` counts the board's postings that are available (catalog.IS_AVAILABLE):
some switched-on source lists them. It is read from source observations, the
same way for every board, and a complete pull of a board that lists every
open posting records what it dropped. An aggregator's absence never closes a
posting, so an aggregator's share stays high. `reports_inactive` says whether
any of the board's postings is unavailable, and the closed-check funnel below
is the instrument that asks the posting itself.

`jobs.created_at` is when a row was loaded into this catalog, not when the
posting was discovered - the whole table shares a floor from the last reseed,
while ai_queries reaches further back. Freshness therefore comes from
date_posted, and `with_date_posted` reports how much of the board that covers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api import db
from api.analytics_assembly import collect_source_analytics as _collect
from api.analytics_models import (
    BoardAnalyticsDetail,
    BoardAnalyticsList,
    OverlapPartner,
)
from api.analytics_queries import _OVERLAP_PARTNERS_SQL
from api.auth import AuthedUser
from api.problem import refuse
from api.rates import DEFAULT_MIN_SAMPLE
from api.routers.admin import require_admin

router = APIRouter(prefix="/analytics")

_CAVEATS = [
    "active counts postings some switched-on source lists. An aggregator's "
    "absence never closes a posting, so compare active_share between company "
    "boards, or between aggregators.",
    "jobs.created_at is when a posting was loaded into this catalog, not when "
    "it was discovered. first_loaded_at is bounded below by the last reseed; "
    "use date_posted for age.",
    "No column records when a posting went inactive, so time-to-inactive and a "
    "decay curve are not computable - decay here is the closed-check verdict.",
    "user_jobs.status carries no interview or offer state, so interview and "
    "offer rates per board cannot be computed from this schema.",
    "Cross-source duplication is keyed on (company, title): jobs.url is unique "
    "table-wide, so the same posting cannot appear under two sources.",
]


@router.get("/sources")
def source_analytics(
    min_sample: int = Query(DEFAULT_MIN_SAMPLE, ge=1, le=10_000),
    user: AuthedUser = Depends(require_admin),
) -> BoardAnalyticsList:
    return BoardAnalyticsList(
        rows=_collect(min_sample),
        min_sample=min_sample,
        caveats=_CAVEATS,
    )


@router.get("/sources/{source}")
def source_detail(
    source: str,
    min_sample: int = Query(DEFAULT_MIN_SAMPLE, ge=1, le=10_000),
    user: AuthedUser = Depends(require_admin),
) -> BoardAnalyticsDetail:
    row = next((r for r in _collect(min_sample) if r.source == source), None)
    if row is None:
        raise refuse(404, "NOT_FOUND", "unknown source")
    return BoardAnalyticsDetail(
        row=row,
        overlap_partners=db.query_as(OverlapPartner, _OVERLAP_PARTNERS_SQL, {"source": source}),
        min_sample=min_sample,
        caveats=_CAVEATS,
    )
