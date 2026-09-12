"""Unauthenticated, allowlisted reads of published managed-board projections."""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel

from api import db
from api.params import csv
from api.routers.job_board import ATS_SQL, AtsName, Openness

router = APIRouter(prefix="/public/job-lists", tags=["public-job-lists"])
PublicSort = Literal["posted", "added", "company", "title", "comp"]
SortDirection = Literal["asc", "desc"]


class PublicJobListLink(BaseModel):
    slug: str
    name: str
    description: str
    job_count: int
    updated_at: datetime.datetime | None


class PublicJobListIndex(BaseModel):
    job_lists: list[PublicJobListLink]


class PublicCompensation(BaseModel):
    minimum: float | None
    maximum: float | None
    currency: str | None
    period: str | None
    basis: str | None
    text: str | None


class PublicJobCard(BaseModel):
    job_id: int
    company: str | None
    title: str | None
    locations: list[str]
    terms: list[str]
    source: str
    ats: AtsName
    date_posted: datetime.datetime | None
    added_at: datetime.datetime
    active: bool
    closed_verdict: Openness | None
    compensation: PublicCompensation
    url: str


class PublicJobList(BaseModel):
    slug: str
    name: str
    description: str
    job_count: int
    updated_at: datetime.datetime | None
    jobs: list[PublicJobCard]
    next_cursor: str | None
    has_more: bool


class PublicJobDetail(BaseModel):
    job: PublicJobCard
    content: str | None
    content_fetched_at: datetime.datetime | None


@dataclass(frozen=True)
class _IndexRow:
    slug: str
    name: str
    description: str
    job_count: int
    updated_at: datetime.datetime | None
    public_revision: int


@dataclass(frozen=True)
class _BoardRow:
    id: int
    slug: str
    name: str
    description: str
    job_count: int
    updated_at: datetime.datetime | None
    public_revision: int


@dataclass(frozen=True)
class _JobRow:
    job_id: int
    sort_value: datetime.datetime | Decimal | str | None
    company: str | None
    title: str | None
    locations: list[str]
    terms: list[str]
    source: str
    ats: AtsName
    date_posted: datetime.datetime | None
    added_at: datetime.datetime
    active: bool
    closed_verdict: Openness | None
    comp_min: Decimal | None
    comp_max: Decimal | None
    comp_currency: str | None
    comp_period: str | None
    comp_basis: str | None
    comp_text: str | None
    url: str


@dataclass(frozen=True)
class _DetailRow(_JobRow):
    content: str | None
    content_fetched_at: datetime.datetime | None


_SORT_EXPRESSIONS: dict[PublicSort, str] = {
    "posted": "j.date_posted",
    "added": "j.created_at",
    "company": "lower(j.company)",
    "title": "lower(j.title)",
    "comp": "j.comp_max",
}


def _etag(parts: list[str]) -> str:
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return f'"{digest}"'


def _not_modified(if_none_match: str | None, etag: str) -> Response | None:
    if if_none_match is not None and etag in {part.strip() for part in if_none_match.split(",")}:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "public, max-age=0, must-revalidate"},
        )
    return None


def _serialize_sort_value(value: datetime.datetime | Decimal | str | None) -> str | None:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _encode_cursor(
    sort: PublicSort,
    direction: SortDirection,
    value: datetime.datetime | Decimal | str | None,
    job_id: int,
) -> str:
    payload = json.dumps(
        {"sort": sort, "dir": direction, "value": _serialize_sort_value(value), "job_id": job_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def _decode_cursor(
    cursor: str, sort: PublicSort, direction: SortDirection
) -> tuple[datetime.datetime | Decimal | str | None, int]:
    try:
        payload = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        parsed = json.loads(payload)
        if (
            set(parsed) != {"sort", "dir", "value", "job_id"}
            or parsed["sort"] != sort
            or parsed["dir"] != direction
            or not isinstance(parsed["job_id"], int)
            or parsed["job_id"] < 1
            or (parsed["value"] is not None and not isinstance(parsed["value"], str))
        ):
            raise ValueError
        raw_value: str | None = parsed["value"]
        value: datetime.datetime | Decimal | str | None = raw_value
        if raw_value is not None and sort in {"posted", "added"}:
            value = datetime.datetime.fromisoformat(raw_value)
            if value.tzinfo is None:
                raise ValueError
        elif raw_value is not None and sort == "comp":
            value = Decimal(raw_value)
        return value, parsed["job_id"]
    except (TypeError, ValueError, KeyError, InvalidOperation, json.JSONDecodeError) as exc:
        raise HTTPException(
            400, detail={"code": "INVALID_CURSOR", "message": "invalid job list cursor"}
        ) from exc


_INDEX_SQL = """
SELECT b.slug, b.name, b.description, count(mj.job_id)::int AS job_count,
       b.projection_updated_at AS updated_at, b.public_revision
FROM managed_boards b
LEFT JOIN managed_board_jobs mj ON mj.managed_board_id = b.id
WHERE b.published
GROUP BY b.id
ORDER BY b.slug
"""


@router.get("", response_model=PublicJobListIndex)
def list_public_job_lists(
    response: Response, if_none_match: str | None = Header(default=None)
) -> PublicJobListIndex | Response:
    rows = db.query_as(_IndexRow, _INDEX_SQL)
    etag = _etag([f"{row.slug}:{row.public_revision}" for row in rows])
    unchanged = _not_modified(if_none_match, etag)
    if unchanged is not None:
        return unchanged
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    return PublicJobListIndex(
        job_lists=[PublicJobListLink.model_validate(row, from_attributes=True) for row in rows]
    )


_BOARD_SQL = """
SELECT b.id, b.slug, b.name, b.description, count(mj.job_id)::int AS job_count,
       b.projection_updated_at AS updated_at, b.public_revision
FROM managed_boards b
LEFT JOIN managed_board_jobs mj ON mj.managed_board_id = b.id
WHERE b.slug = %s AND b.published
GROUP BY b.id
"""

_CARD_COLUMNS = f"""
mj.job_id, {{sort_expression}} AS sort_value, j.company, j.title, j.locations, j.terms,
j.source, ({ATS_SQL}) AS ats, j.date_posted, j.created_at AS added_at, j.active,
(SELECT CASE q.status WHEN 'passed' THEN 'open' WHEN 'rejected' THEN 'closed' END
 FROM ai_queries q
 WHERE q.url = j.url AND q.check_type = 'closed' AND q.status IN ('passed', 'rejected')
 ORDER BY q.id DESC LIMIT 1) AS closed_verdict,
j.comp_min, j.comp_max, j.comp_currency, j.comp_period, j.comp_basis, j.comp_text, j.url
"""


def _card(row: _JobRow) -> PublicJobCard:
    return PublicJobCard(
        job_id=row.job_id,
        company=row.company,
        title=row.title,
        locations=row.locations,
        terms=row.terms,
        source=row.source,
        ats=row.ats,
        date_posted=row.date_posted,
        added_at=row.added_at,
        active=row.active,
        closed_verdict=row.closed_verdict,
        compensation=PublicCompensation(
            minimum=float(row.comp_min) if row.comp_min is not None else None,
            maximum=float(row.comp_max) if row.comp_max is not None else None,
            currency=row.comp_currency,
            period=row.comp_period,
            basis=row.comp_basis,
            text=row.comp_text,
        ),
        url=row.url,
    )


def _cursor_predicate(
    expression: str, direction: SortDirection, value: datetime.datetime | Decimal | str | None
) -> str:
    comparator = ">" if direction == "asc" else "<"
    if value is None:
        return f"AND {expression} IS NULL AND mj.job_id {comparator} %(cursor_id)s"
    return f"AND ({expression} IS NULL OR {expression} {comparator} %(cursor_value)s OR ({expression} = %(cursor_value)s AND mj.job_id {comparator} %(cursor_id)s))"


@router.get("/{slug}", response_model=PublicJobList)
def get_public_job_list(
    slug: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    sort: PublicSort = "posted",
    dir: SortDirection = "desc",
    q: str | None = Query(default=None, max_length=200),
    terms: str | None = None,
    if_none_match: str | None = Header(default=None),
) -> PublicJobList | Response:
    wanted_terms = csv(terms)
    expression = _SORT_EXPRESSIONS[sort]
    cursor_value, cursor_id = _decode_cursor(cursor, sort, dir) if cursor else (None, None)
    filters: list[str] = []
    params: dict[str, object] = {"fetch_limit": limit + 1}
    if q:
        filters.append("AND (j.company ILIKE %(q)s OR j.title ILIKE %(q)s OR j.url ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if wanted_terms:
        filters.append("AND j.terms && %(terms)s")
        params["terms"] = wanted_terms
    if cursor:
        filters.append(_cursor_predicate(expression, dir, cursor_value))
        params.update(cursor_value=cursor_value, cursor_id=cursor_id)
    order = "ASC" if dir == "asc" else "DESC"
    jobs_sql = f"""
    SELECT {_CARD_COLUMNS.format(sort_expression=expression)}
    FROM managed_board_jobs mj
    JOIN jobs j ON j.id = mj.job_id
    JOIN managed_boards b ON b.id = mj.managed_board_id AND b.published
    WHERE mj.managed_board_id = %(board_id)s {" ".join(filters)}
    ORDER BY {expression} {order} NULLS LAST, mj.job_id {order}
    LIMIT %(fetch_limit)s
    """
    with db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        board = db.query_one_as(_BoardRow, _BOARD_SQL, (slug,))
        if board is None:
            raise HTTPException(
                404,
                detail={"code": "NOT_FOUND", "message": "unknown job list"},
                headers={"Cache-Control": "no-store"},
            )
        params["board_id"] = board.id
        rows = db.query_as(_JobRow, jobs_sql, params)
    page = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = (
        _encode_cursor(sort, dir, page[-1].sort_value, page[-1].job_id) if has_more else None
    )
    cards = [_card(row) for row in page]
    # Projection revisions identify membership, but posting facts and closure
    # evidence can change between projection runs. Hash the rendered page so a
    # revalidation cannot preserve stale public facts under an unchanged revision.
    etag = _etag(
        [
            board.slug,
            str(board.public_revision),
            cursor or "",
            str(limit),
            sort,
            dir,
            q or "",
            "\0".join(wanted_terms),
            next_cursor or "",
            *(card.model_dump_json() for card in cards),
        ]
    )
    unchanged = _not_modified(if_none_match, etag)
    if unchanged is not None:
        return unchanged
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    return PublicJobList(
        slug=board.slug,
        name=board.name,
        description=board.description,
        job_count=board.job_count,
        updated_at=board.updated_at,
        jobs=cards,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/{slug}/jobs/{job_id}", response_model=PublicJobDetail)
def get_public_job(slug: str, job_id: int, response: Response) -> PublicJobDetail:
    detail_sql = f"""
    SELECT {_CARD_COLUMNS.format(sort_expression="mj.sort_at")},
           content.input_content AS content, content.created_at AS content_fetched_at
    FROM managed_board_jobs mj
    JOIN jobs j ON j.id = mj.job_id
    JOIN managed_boards b ON b.id = mj.managed_board_id AND b.published
    LEFT JOIN LATERAL (
        SELECT q.input_content, q.created_at FROM ai_queries q
        WHERE q.url = j.url AND q.check_type = 'content' AND q.status = 'passed'
          AND q.input_content IS NOT NULL
        ORDER BY q.id DESC LIMIT 1
    ) content ON TRUE
    WHERE b.slug = %s AND mj.job_id = %s
    """
    row = db.query_one_as(_DetailRow, detail_sql, (slug, job_id))
    if row is None:
        raise HTTPException(
            404,
            detail={"code": "NOT_FOUND", "message": "unknown job"},
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    return PublicJobDetail(
        job=_card(row), content=row.content, content_fetched_at=row.content_fetched_at
    )
