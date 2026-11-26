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

from typing import Literal

from fastapi import APIRouter, Depends, Query

from api.analytics.assembly import (
    collect_source_analytics,
    source_connection,
)
from api.analytics.assembly import (
    source_detail as board_detail,
)
from api.analytics.models import (
    BoardAnalyticsDetail,
    BoardAnalyticsList,
)
from api.auth import AuthedUser
from api.connections import MAX_FIRST, PageInfo, cursor_for, decode_cursor
from api.problem import refuse
from api.rates import DEFAULT_MIN_SAMPLE
from api.routers.admin import require_admin

router = APIRouter(prefix="/analytics")

# Boards with no postings named above the table; the rest are counted, as the
# page did when a bundle of a few hundred boards switched on at once.
NO_POSTINGS_NAMED = 10

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


def _is_position(position: object, sort: str) -> bool:
    """A position source_connection could have written for this sort."""
    if not isinstance(position, list):
        return False
    if sort == "source":
        return len(position) == 2 and all(isinstance(p, str) for p in position)
    return (
        len(position) == 3
        and position[0] in (0, 1)
        and isinstance(position[1], int | float)
        and not isinstance(position[1], bool)
        and isinstance(position[2], str)
    )


@router.get("/sources")
def source_analytics(
    first: int = Query(ge=1, le=MAX_FIRST),
    after: str | None = None,
    min_sample: int = Query(DEFAULT_MIN_SAMPLE, ge=1, le=10_000),
    q: str | None = Query(default=None, max_length=200),
    sort: Literal["source", "total", "still_open", "doa", "clearance", "cost", "apply"] = "total",
    dir: Literal["asc", "desc"] = "desc",
    user: AuthedUser = Depends(require_admin),
) -> BoardAnalyticsList:
    """The boards as a connection (api.connections): the `first` that come
    after the cursor `after`, searched and sorted over every board."""
    query = {"q": (q or "").strip().lower(), "sort": sort, "dir": dir, "min_sample": min_sample}
    position = decode_cursor(after, query) if after else None
    if position is not None and not _is_position(position, sort):
        raise refuse(400, "INVALID_CURSOR", "the cursor is not from this query")
    rows = collect_source_analytics(min_sample)
    nodes, end, more, total = source_connection(
        rows,
        q=q,
        sort=sort,
        descending=dir == "desc",
        first=first,
        after=tuple(position) if position is not None else None,
    )
    silent = [r.source for r in rows if r.inventory.total == 0]
    return BoardAnalyticsList(
        nodes=nodes,
        page_info=PageInfo(
            end_cursor=cursor_for(query, list(end)) if end is not None else None,
            has_next_page=more,
        ),
        total_count=total,
        min_sample=min_sample,
        caveats=_CAVEATS,
        no_postings=silent[:NO_POSTINGS_NAMED],
        no_postings_total=len(silent),
    )


@router.get("/sources/{source}")
def source_detail(
    source: str,
    min_sample: int = Query(DEFAULT_MIN_SAMPLE, ge=1, le=10_000),
    user: AuthedUser = Depends(require_admin),
) -> BoardAnalyticsDetail:
    found = board_detail(source, min_sample)
    if found is None:
        raise refuse(404, "NOT_FOUND", "unknown source")
    row, partners = found
    return BoardAnalyticsDetail(
        row=row,
        overlap_partners=partners,
        min_sample=min_sample,
        caveats=_CAVEATS,
    )
