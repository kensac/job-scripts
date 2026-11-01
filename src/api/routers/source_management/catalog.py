"""Admin source catalog writes and source row validation."""

from __future__ import annotations

import datetime
import re
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import db, source_selection
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin import require_admin
from api.updates import NonNullUpdate
from core.fetching import boards

router = APIRouter()


class CatalogSource(BaseModel):
    """A source row as stored, plus the format read off its URL. `kind` is
    never a column: it is derived from `listings_url` every time, so it cannot
    drift from what ingest will do with the row."""

    name: str
    listings_url: str
    description: str
    active: bool
    created_at: datetime.datetime
    company: str | None
    title_pattern: str | None
    ingest_interval_hours: int
    kind: str


class Attached(BaseModel):
    """What still hangs off a source, and therefore what deleting it would
    orphan."""

    jobs: int
    subscribers: int
    board_rows: int
    managed_boards: int


class _ManagedBoardId(BaseModel):
    id: int


class SourceDeleted(BaseModel):
    ok: bool
    deleted: str
    was_attached: Attached


_SOURCE_COLS = (
    "name, listings_url, description, active, created_at, company, title_pattern, "
    "ingest_interval_hours"
)


class SourceBody(BaseModel):
    name: NonNullUpdate[Annotated[str, Field(min_length=1, max_length=80)]] = None
    listings_url: NonNullUpdate[Annotated[str, Field(max_length=1000)]] = None
    description: NonNullUpdate[Annotated[str, Field(max_length=500)]] = None
    active: NonNullUpdate[bool] = None
    company: str | None = Field(default=None, max_length=200)
    # Bounded by _check_source, which lets a stored pattern through unchanged.
    title_pattern: str | None = None
    # Hours between pulls; 1 is the hourly cycle. Bounded above by a week so a
    # typo cannot park a board for a year while it reads as active.
    ingest_interval_hours: NonNullUpdate[Annotated[int, Field(ge=1, le=168)]] = None


# The limit since #315. It binds what an admin writes, not what is stored:
# 2,483 sources held one 583-character pattern on 2026-10-10, and an edit that
# leaves a stored pattern as it is must not be refused for its length.
TITLE_PATTERN_MAX = 500


def check_pattern_length(title_pattern: str | None, stored: str | None) -> None:
    if title_pattern and title_pattern != stored and len(title_pattern) > TITLE_PATTERN_MAX:
        raise refuse(
            400,
            "TITLE_PATTERN_TOO_LONG",
            f"title_pattern: at most {TITLE_PATTERN_MAX} characters",
        )


def _check_source(
    listings_url: str, company: str | None, title_pattern: str | None, stored: str | None = None
) -> None:
    """The facts a source row can get wrong silently: a company board on a
    system that never names the company, and a pattern that ingest cannot
    compile. Both would surface only as a failed ingest an hour later.
    `stored` is the pattern the row holds now, which is valid as it is."""
    check_pattern_length(title_pattern, stored)
    board_format = boards.format_for_url(listings_url)
    if board_format.needs_company and not (company or "").strip():
        raise refuse(
            400,
            "COMPANY_REQUIRED",
            f"a {board_format.name} board never names its company; "
            "set company to the employer it belongs to",
        )
    if title_pattern:
        try:
            re.compile(title_pattern, re.IGNORECASE)
        except re.error as exc:
            raise refuse(400, "BAD_TITLE_PATTERN", f"title_pattern: {exc}") from exc


def check_one_active_per_board(turning_on: dict[str, str]) -> None:
    """Refuses to make these sources (name to listings URL) active when another
    active source, or another of them, reads the same board. Workday and Ashby
    read the path without regard to case, so a case-only difference is the same
    board, and its postings get different URLs: each is verified, filtered and
    shown twice. On 2026-10-18, 91 boards were read by two or more sources."""
    clash = db.query_one(
        "SELECT c.name, s.name AS other FROM unnest(%(names)s::text[], %(urls)s::text[]) "
        "c(name, url) JOIN sources s ON lower(s.listings_url) = lower(c.url) "
        "AND s.name <> c.name AND (s.active OR s.name = ANY(%(names)s)) "
        "ORDER BY c.name, s.name LIMIT 1",
        {"names": list(turning_on), "urls": list(turning_on.values())},
    )
    if clash:
        raise refuse(
            409,
            "DUPLICATE_BOARD",
            f"{clash['name']} reads the same board as active source {clash['other']}; "
            "switch one off",
            source=clash["name"],
            other=clash["other"],
        )


@router.post("/sources")
def create_source(body: SourceBody, user: AuthedUser = Depends(require_admin)) -> CatalogSource:
    if not body.name or not body.listings_url:
        raise refuse(400, "MISSING_FIELDS", "name and listings_url are required")
    _check_source(body.listings_url, body.company, body.title_pattern)
    if db.query_one("SELECT name FROM sources WHERE name = %s", (body.name,)):
        raise refuse(409, "DUPLICATE_NAME", "source name exists")
    if body.active is not False:
        check_one_active_per_board({body.name: body.listings_url})
    row = db.query_one(
        "INSERT INTO sources (name, listings_url, description, active, company, title_pattern, "
        f"ingest_interval_hours) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_SOURCE_COLS}",
        (
            body.name,
            body.listings_url,
            body.description or "",
            body.active if body.active is not None else True,
            (body.company or "").strip() or None,
            (body.title_pattern or "").strip() or None,
            body.ingest_interval_hours or 1,
        ),
    )
    assert row is not None  # an insert with RETURNING always yields its row
    return CatalogSource(**row, kind=boards.kind(row["listings_url"]))


@router.delete("/sources/{name}")
def delete_source(
    name: str, force: bool = False, user: AuthedUser = Depends(require_admin)
) -> SourceDeleted:
    """Permanently remove a source. Refuses while anything still hangs off it.
    Jobs would be orphaned into a source that no longer exists, and there is no
    undo, so emptiness is proven rather than assumed. force=true is the
    deliberate override. Group memberships are always cleaned up, because a
    group pointing at a deleted source is silent debris."""
    source_row = db.query_one("SELECT name FROM sources WHERE name = %s", (name,))
    if not source_row:
        raise refuse(404, "NOT_FOUND", "unknown source")
    attached = db.query_one(
        """
        SELECT (SELECT count(*) FROM jobs WHERE source = %(n)s) AS jobs,
               (SELECT count(*) FROM user_source_set WHERE source = %(n)s) AS subscribers,
               (SELECT count(*) FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
                WHERE j.source = %(n)s) AS board_rows,
               (SELECT count(*) FROM managed_board_sources
                WHERE source = %(n)s) AS managed_boards
        """,
        {"n": name},
    )
    # The aggregate always returns exactly one row, but assert it rather than
    # subscript an Optional - a silent None here would 500 mid-delete.
    assert attached is not None
    if not force and any(attached.values()):
        raise refuse(
            409,
            "SOURCE_IN_USE",
            f"{name} still has {attached['jobs']} jobs, "
            f"{attached['subscribers']} subscribers, {attached['board_rows']} board rows, "
            f"{attached['managed_boards']} managed boards",
            attached=attached,
        )
    with db.transaction():
        affected_boards = db.query_as(
            _ManagedBoardId,
            "SELECT b.id FROM managed_boards b JOIN managed_board_sources s "
            "ON s.managed_board_id = b.id WHERE s.source = %s ORDER BY b.id FOR UPDATE OF b",
            (name,),
        )
        if affected_boards:
            db.execute(
                "UPDATE managed_boards SET revision = revision + 1, "
                "public_revision = CASE WHEN published THEN COALESCE(public_revision, 0) + 1 "
                "ELSE public_revision END, updated_at = now() WHERE id = ANY(%s)",
                ([board.id for board in affected_boards],),
            )
        source_selection.forget_source(name)
        db.execute("DELETE FROM sources WHERE name = %s", (name,))
    return SourceDeleted(ok=True, deleted=name, was_attached=Attached(**attached))


@router.get("/sources/{name}")
def get_source(name: str, user: AuthedUser = Depends(require_admin)) -> CatalogSource:
    """One source as stored, title_pattern included; the list shape omits it."""
    row = db.query_one(f"SELECT {_SOURCE_COLS} FROM sources WHERE name = %s", (name,))
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown source")
    return CatalogSource(**row, kind=boards.kind(row["listings_url"]))


@router.patch("/sources/{name}")
def patch_source(
    name: str, body: SourceBody, user: AuthedUser = Depends(require_admin)
) -> CatalogSource:
    fields = body.model_dump(exclude_unset=True, exclude={"name"})
    if not fields:
        raise refuse(400, "EMPTY_PATCH", "no fields to update")
    for k in ("company", "title_pattern"):
        if k in fields:
            fields[k] = (fields[k] or "").strip() or None
    current = db.query_one(f"SELECT {_SOURCE_COLS} FROM sources WHERE name = %s", (name,))
    if not current:
        raise refuse(404, "NOT_FOUND", "unknown source")
    merged = {**current, **fields}
    _check_source(
        merged["listings_url"], merged["company"], merged["title_pattern"], current["title_pattern"]
    )
    if merged["active"] and ("active" in fields or "listings_url" in fields):
        check_one_active_per_board({name: merged["listings_url"]})
    assignments = ", ".join(f"{column} = %({column})s" for column in fields)
    row = db.query_one(
        f"UPDATE sources SET {assignments} WHERE name = %(name)s RETURNING {_SOURCE_COLS}",
        {"name": name, **fields},
    )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown source")
    return CatalogSource(**row, kind=boards.kind(row["listings_url"]))
