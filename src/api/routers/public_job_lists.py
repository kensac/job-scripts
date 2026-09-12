"""Unauthenticated, allowlisted reads of published managed-board projections."""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel

from api import db

router = APIRouter(prefix="/public/job-lists", tags=["public-job-lists"])


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
    company: str | None
    title: str | None
    locations: list[str]
    date_posted: datetime.datetime | None
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
    sort_at: datetime.datetime
    company: str | None
    title: str | None
    locations: list[str]
    date_posted: datetime.datetime | None
    comp_min: Decimal | None
    comp_max: Decimal | None
    comp_currency: str | None
    comp_period: str | None
    comp_basis: str | None
    comp_text: str | None
    url: str


def _etag(parts: list[str]) -> str:
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return f'"{digest}"'


def _not_modified(if_none_match: str | None, etag: str) -> Response | None:
    if if_none_match is not None and etag in {part.strip() for part in if_none_match.split(",")}:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "public, max-age=0, must-revalidate",
            },
        )
    return None


def _encode_cursor(sort_at: datetime.datetime, job_id: int) -> str:
    payload = json.dumps(
        {"sort_at": sort_at.isoformat(), "job_id": job_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> tuple[datetime.datetime, int]:
    try:
        payload = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        parsed = json.loads(payload)
        if set(parsed) != {"sort_at", "job_id"} or not isinstance(parsed["job_id"], int):
            raise ValueError
        sort_at = datetime.datetime.fromisoformat(parsed["sort_at"])
        if sort_at.tzinfo is None or parsed["job_id"] < 1:
            raise ValueError
        return sort_at, parsed["job_id"]
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
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

_JOBS_SQL = """
SELECT mj.job_id, mj.sort_at, j.company, j.title, j.locations, j.date_posted,
       j.comp_min, j.comp_max, j.comp_currency, j.comp_period, j.comp_basis,
       j.comp_text, j.url
FROM managed_board_jobs mj
JOIN jobs j ON j.id = mj.job_id
JOIN managed_boards b ON b.id = mj.managed_board_id AND b.published
WHERE mj.managed_board_id = %(board_id)s
  AND (%(cursor_at)s::timestamptz IS NULL
       OR (mj.sort_at, mj.job_id) < (%(cursor_at)s, %(cursor_id)s))
ORDER BY mj.sort_at DESC, mj.job_id DESC
LIMIT %(fetch_limit)s
"""


@router.get("/{slug}", response_model=PublicJobList)
def get_public_job_list(
    slug: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    if_none_match: str | None = Header(default=None),
) -> PublicJobList | Response:
    with db.transaction():
        # Metadata and projection rows must describe one publication state if
        # an administrator unpublishes or replaces a projection mid-request.
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        board = db.query_one_as(_BoardRow, _BOARD_SQL, (slug,))
        if board is None:
            raise HTTPException(
                404,
                detail={"code": "NOT_FOUND", "message": "unknown job list"},
                headers={"Cache-Control": "no-store"},
            )

        cursor_at, cursor_id = _decode_cursor(cursor) if cursor else (None, None)
        etag = _etag([board.slug, str(board.public_revision), cursor or "", str(limit)])
        unchanged = _not_modified(if_none_match, etag)
        if unchanged is not None:
            return unchanged
        rows = db.query_as(
            _JobRow,
            _JOBS_SQL,
            {
                "board_id": board.id,
                "cursor_at": cursor_at,
                "cursor_id": cursor_id,
                "fetch_limit": limit + 1,
            },
        )
    page = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = _encode_cursor(page[-1].sort_at, page[-1].job_id) if has_more else None
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    return PublicJobList(
        slug=board.slug,
        name=board.name,
        description=board.description,
        job_count=board.job_count,
        updated_at=board.updated_at,
        jobs=[
            PublicJobCard(
                company=row.company,
                title=row.title,
                locations=row.locations,
                date_posted=row.date_posted,
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
            for row in page
        ],
        next_cursor=next_cursor,
        has_more=has_more,
    )
