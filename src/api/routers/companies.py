"""Administrative company name analytics route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api import db, rates, signals, sorting
from api import params as params_
from api.auth import AuthedUser
from api.companies.models import (
    CompanyCoverage,
    CompanyList,
    _CompanyRow,
    _Count,
    _CurrencyRow,
    _OpenRow,
    _RepostRow,
    _ResponseRow,
    _StatusRow,
)
from api.companies.projection import _bucket, _item
from api.companies.queries import (
    _BASE_SQL,
    _CAVEATS,
    _COUNT_SQL,
    _CURRENCY_SQL,
    _CUTS,
    _OPEN_SQL,
    _OUTCOME_KINDS,
    _REPOST_SQL,
    _RESPONSE_SQL,
    _STATUS_SQL,
)
from api.mail import pipeline as mail_pipeline
from api.problem import refuse
from api.routers.admin import require_admin
from api.stale_cache import StaleByKey

router = APIRouter(prefix="/admin")

_SORTABLE = {
    "company_name": "company_name",
    "total_postings_seen": "total_postings_seen",
    "comp_extracted_n": "comp_found",
    "applications": "applications_n",
}

_MAX_LIMIT = 200


def _offset_from(cursor: str | None) -> int:
    """The cursor is an offset, and says so rather than pretending to be a
    keyset. Sorting is over a whole-table aggregate that has to be recomputed
    per page anyway, so a keyset would buy nothing and would break the moment
    the caller sorted on a non-unique column."""
    if not cursor:
        return 0
    try:
        offset = int(cursor)
    except ValueError as exc:
        raise refuse(400, "BAD_CURSOR", "cursor must be an offset") from exc
    if offset < 0:
        raise refuse(400, "BAD_CURSOR", "cursor must not be negative")
    return offset


@router.get("/companies")
def list_companies(
    q: str | None = None,
    applied: bool = False,
    has_comp: bool = False,
    repost: bool = False,
    sort: str = "total_postings_seen",
    dir: str = "desc",
    limit: int = 50,
    cursor: str | None = None,
    min_sample: int = Query(default=rates.DEFAULT_MIN_SAMPLE, ge=1),
    user: AuthedUser = Depends(require_admin),
) -> CompanyList:
    """Served stale for admin_stats_cache_seconds per query and admin: the
    page groups the whole catalog and took 30 s on production (2026-10-13)."""
    args = (q, applied, has_comp, repost, sort, dir, limit, cursor, min_sample)
    return _pages.get((*args, user.id), lambda: _list_companies(*args, user=user))


_pages: StaleByKey[tuple, CompanyList] = StaleByKey(
    lambda: int(db.get_config("admin_stats_cache_seconds"))
)


def _list_companies(
    q: str | None,
    applied: bool,
    has_comp: bool,
    repost: bool,
    sort: str,
    dir: str,
    limit: int,
    cursor: str | None,
    min_sample: int,
    *,
    user: AuthedUser,
) -> CompanyList:
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = _offset_from(cursor)
    # Sorting is applied to the whole aggregate before the page is cut, not
    # within the slice - a page-local sort would reorder 50 rows and call it a
    # ranking of 7,564.
    sorts = sorting.parse(sort, dir, _SORTABLE, "total_postings_seen")
    order = sorting.clause(sorts, _SORTABLE)
    pattern = f"%{q.strip().lower()}%" if q and q.strip() else None
    params = {
        "q": pattern,
        "limit": limit + 1,
        "offset": offset,
        "user_id": user.id,
        "applied": applied,
        "has_comp": has_comp,
        "repost": repost,
        "min_urls": signals.REPOST_MIN_URLS,
        "min_span": signals.REPOST_MIN_SPAN_DAYS,
    }

    rows = db.query_as(_CompanyRow, _BASE_SQL.format(order=order, cuts=_CUTS), params)
    has_more = len(rows) > limit
    rows = rows[:limit]
    keys = [r.company_key for r in rows]

    currency = _bucket(db.query_as(_CurrencyRow, _CURRENCY_SQL, {"keys": keys})) if keys else {}
    statuses = (
        _bucket(db.query_as(_StatusRow, _STATUS_SQL, {"keys": keys, "user_id": user.id}))
        if keys
        else {}
    )
    open_rows = (
        {r.company_key: r for r in db.query_as(_OpenRow, _OPEN_SQL, {"keys": keys})} if keys else {}
    )
    reposts = (
        {
            r.company_key: r
            for r in db.query_as(
                _RepostRow,
                _REPOST_SQL,
                {
                    "keys": keys,
                    "min_urls": signals.REPOST_MIN_URLS,
                    "min_span": signals.REPOST_MIN_SPAN_DAYS,
                },
            )
        }
        if keys
        else {}
    )

    responses = (
        {
            r.company_key: r
            for r in db.query_as(
                _ResponseRow,
                _RESPONSE_SQL,
                {
                    "keys": keys,
                    "user_id": user.id,
                    "outcomes": list(_OUTCOME_KINDS),
                    "intermediaries": mail_pipeline.intermediary_domains(),
                },
            )
        }
        if keys
        else {}
    )

    if rows:
        total_names = rows[0].total_names
    elif offset:
        total_row = db.query_one_as(_Count, _COUNT_SQL.format(cuts=_CUTS), params)
        total_names = total_row.c if total_row else 0
    else:
        total_names = 0
    awaiting = db.query_one_as(
        _Count,
        "SELECT count(*) AS c FROM email_messages m WHERE NOT EXISTS "
        "(SELECT 1 FROM email_events e WHERE e.message_id = m.id)",
    )
    return CompanyList(
        items=[
            _item(
                row,
                currency=currency.get(row.company_key, []),
                statuses=statuses.get(row.company_key, []),
                open_row=open_rows.get(row.company_key),
                repost=reposts.get(row.company_key),
                responses=responses.get(row.company_key),
                min_sample=min_sample,
            )
            for row in rows
        ],
        has_more=has_more,
        next_cursor=str(offset + limit) if has_more else None,
        coverage=CompanyCoverage(
            messages_awaiting_classification=awaiting.c if awaiting else 0,
            min_sample=min_sample,
        ),
        total_names=total_names,
        caveats=_CAVEATS,
        filters=params_.applied(
            q=[q.strip()] if q and pattern else [],
            applied=["true"] if applied else [],
            has_comp=["true"] if has_comp else [],
            repost=["true"] if repost else [],
        ),
        filterable=["q", "applied", "has_comp", "repost"],
        sorts=sorts,
        sortable=sorted(_SORTABLE),
    )
