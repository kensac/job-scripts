"""Admin previews and reads for source title screening."""

from __future__ import annotations

import datetime
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin import require_admin
from api.routers.source_management.catalog import _check_pattern_length
from core import catalog

router = APIRouter()


class PatternSamples(BaseModel):
    admitted: list[str]
    excluded: list[str]


class PatternPreview(BaseModel):
    """What a candidate pattern would admit, against every title this board
    has listed. `would_add` is screened titles it lets in, `would_drop` is
    catalog titles it turns away, both among the titles the board lists now.
    Nothing is written."""

    source: str
    title_pattern: str
    titles: int
    admitted: int
    excluded: int
    would_add: int
    would_drop: int
    samples: PatternSamples


class ScreenedPosting(BaseModel):
    url: str
    company: str
    title: str
    locations: list[str]
    date_posted: datetime.datetime | None
    pattern: str
    first_seen_at: datetime.datetime
    last_seen_at: datetime.datetime


class ScreenedPostings(BaseModel):
    source: str
    rows: list[ScreenedPosting]
    total: int
    has_more: bool


class PatternPreviewBody(BaseModel):
    # Bounded like an edit: the stored pattern previews at any length.
    title_pattern: str
    # How many example titles to return on each side.
    samples: int = Field(default=25, ge=0, le=200)


@router.post("/sources/{name}/pattern-preview")
def pattern_preview(
    name: str, body: PatternPreviewBody, user: AuthedUser = Depends(require_admin)
) -> PatternPreview:
    """What a candidate title pattern would admit, judged against every
    title this board has listed: the ones in the catalog and the ones the
    current pattern screened out. Nothing is written. The pattern that goes
    live is whichever one the admin chooses after seeing both sides."""
    source = db.query_one("SELECT title_pattern FROM sources WHERE name = %s", (name,))
    if not source:
        raise refuse(404, "NOT_FOUND", "unknown source")
    _check_pattern_length(body.title_pattern, source["title_pattern"])
    try:
        candidate = re.compile(body.title_pattern, re.IGNORECASE)
    except re.error as exc:
        raise refuse(400, "BAD_TITLE_PATTERN", f"title_pattern: {exc}") from exc
    titles = db.query(
        f"""
        SELECT l.url, l.title, CASE WHEN l.kept THEN 'catalog' ELSE 'screened' END AS held_in,
               {catalog.LISTED_NOW.format(listing="l", source="%(name)s")} AS listed_now
        FROM listings l WHERE l.source = %(name)s
        ORDER BY l.title
        """,
        {"name": name, "refresh_hours": int(db.get_config("listings_seen_refresh_hours"))},
    )
    admitted = [title_row for title_row in titles if candidate.search(title_row["title"])]
    excluded = [title_row for title_row in titles if not candidate.search(title_row["title"])]
    # The catalog side is what the LIVE pattern admitted; a candidate that
    # excludes some of it is narrowing, one that admits screened rows is
    # widening. Both counts, so the change reads as what it is. Both count
    # only titles the board lists now: the next pull acts on nothing else.
    return PatternPreview(
        source=name,
        title_pattern=body.title_pattern,
        titles=len(titles),
        admitted=len(admitted),
        excluded=len(excluded),
        would_add=sum(
            1
            for title_row in admitted
            if title_row["listed_now"] and title_row["held_in"] == "screened"
        ),
        would_drop=sum(
            1
            for title_row in excluded
            if title_row["listed_now"] and title_row["held_in"] == "catalog"
        ),
        samples=PatternSamples(
            admitted=[title_row["title"] for title_row in admitted[: body.samples]],
            excluded=[title_row["title"] for title_row in excluded[: body.samples]],
        ),
    )


@router.get("/sources/{name}/screened")
def screened_postings(
    name: str, limit: int = 100, offset: int = 0, user: AuthedUser = Depends(require_admin)
) -> ScreenedPostings:
    """The postings this board lists that its title pattern did not admit,
    newest listing first, so an admin can see what a pattern is costing."""
    limit = max(1, min(limit, 500))
    # Listings outlive the board's listing of them, so "lists" is LISTED_NOW.
    listed_now = catalog.LISTED_NOW.format(listing="l", source="%(name)s")
    params = {
        "name": name,
        "refresh_hours": int(db.get_config("listings_seen_refresh_hours")),
        "limit": limit,
        "offset": max(0, offset),
    }
    total = db.query_one(
        f"SELECT count(*) AS n FROM listings l "
        f"WHERE l.source = %(name)s AND NOT l.kept AND {listed_now}",
        params,
    )
    rows = db.query_as(
        ScreenedPosting,
        "SELECT l.url, l.company, l.title, l.locations, l.date_posted, t.pattern, "
        "l.first_seen_at, l.last_seen_at "
        "FROM listings l JOIN title_patterns t ON t.id = l.pattern_id "
        f"WHERE l.source = %(name)s AND NOT l.kept AND {listed_now} "
        "ORDER BY l.date_posted DESC NULLS LAST, l.title LIMIT %(limit)s OFFSET %(offset)s",
        params,
    )
    n = total["n"] if total else 0
    return ScreenedPostings(source=name, rows=rows, total=n, has_more=offset + len(rows) < n)
