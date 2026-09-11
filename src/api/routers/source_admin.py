"""Administering the source catalog: the rows that decide what gets pulled.

Split out of admin.py rather than added to it, the same way spend.py and
filter_insights.py were. When spend.py left, admin.py was 1,466 lines; it had
reached 2,534 with the pattern still written down and no longer followed.

A source is a row, never a code path, so everything here edits rows: the
listings URL picks the format, `title_pattern` gates which titles enter the
catalog, and `active` stops the scrape and every AI check on that board
without touching anyone's subscription to it. See
docs/agents/sources-and-boards.md.

The user-facing side of sources lives in routers/sources.py: a person joining
or leaving a board is a different subject from the catalog those boards are
listed in, and the two are not one module.
"""

from __future__ import annotations

import datetime
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser
from api.routers.admin import require_admin
from core.fetching import boards

logger = logging.getLogger("jobtracker_api")

router = APIRouter(prefix="/admin")


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


class SourceName(BaseModel):
    """The vocabulary a picker needs, and nothing that costs a join over
    jobs."""

    name: str
    active: bool
    groups: list[str]
    kind: str


class SourceNames(BaseModel):
    sources: list[SourceName]


class CatalogCounts(BaseModel):
    """What a dashboard tile needs. `by_kind` counts active sources per board
    format; `last_ingest` counts sources by how their most recent pull ended."""

    sources: int
    active: int
    by_kind: dict[str, int]
    last_ingest: dict[str, int]
    bundles: int


class Ingesting(BaseModel):
    """The pull queued or running for a board, so the page can disable its
    button from server state rather than guessing."""

    id: int
    status: str
    created_at: datetime.datetime


class SourceLedger(BaseModel):
    """One row of the full list. `title_pattern` is deliberately absent: it is
    a regex repeated on most rows and only the edit form reads it, through
    GET /admin/sources/{name}.

    `last_ingest_at` says the fetch worked. `last_new_posting_at` says the
    fetch found something we had not already seen. The gap between them is
    what retires a source."""

    name: str
    listings_url: str
    description: str
    active: bool
    created_at: datetime.datetime
    company: str | None
    ingest_interval_hours: int
    groups: list[str]
    jobs: int
    subscribers: int
    last_ingest_status: str | None
    last_ingest_at: datetime.datetime | None
    last_ingest_error: str | None
    last_new_posting_at: datetime.datetime | None
    kind: str
    task: Ingesting | None


class SourceLedgers(BaseModel):
    sources: list[SourceLedger]


class Attached(BaseModel):
    """What still hangs off a source, and therefore what deleting it would
    orphan."""

    jobs: int
    subscribers: int
    board_rows: int


class SourceDeleted(BaseModel):
    ok: bool
    deleted: str
    was_attached: Attached


class SourcesSwitched(BaseModel):
    """What was asked for, what the selection resolved to, and which rows
    actually moved. `selected` and `changed` differ by the rows that already
    held the value."""

    active: bool | None
    ingest_interval_hours: int | None
    selected: list[str]
    changed: list[str]


class PatternSamples(BaseModel):
    admitted: list[str]
    excluded: list[str]


class PatternPreview(BaseModel):
    """What a candidate pattern would admit, against every title this board
    has listed. `would_add` is screened titles it lets in, `would_drop` is
    catalog titles it turns away. Nothing is written."""

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


@router.get("/sources")
def admin_list_sources(
    shape: str = "full", user: AuthedUser = Depends(require_admin)
) -> SourceNames | CatalogCounts | SourceLedgers:
    """shape=full is the ledger. shape=names is the vocabulary (name, active,
    kind, bundles) for pickers and filters; shape=counts is what a dashboard
    tile needs. Both skip the per-source aggregation over jobs, tasks and
    subscribers, which the dashboard was paying for on every live tick."""
    if shape == "names":
        rows = db.query(
            """
            SELECT s.name, s.listings_url, s.active,
                   COALESCE((SELECT array_agg(g.name ORDER BY g.name) FROM source_groups g
                             WHERE s.name = ANY(g.members)), '{}') AS groups
            FROM sources s ORDER BY s.active DESC, s.name
            """
        )
        return SourceNames(
            sources=[
                SourceName(
                    name=r["name"],
                    active=r["active"],
                    groups=r["groups"],
                    kind=boards.kind(r["listings_url"]),
                )
                for r in rows
            ]
        )
    if shape == "counts":
        total = db.query_one(
            "SELECT COUNT(*) AS sources, COUNT(*) FILTER (WHERE active) AS active FROM sources"
        )
        by_kind: dict[str, int] = {}
        for r in db.query("SELECT listings_url FROM sources WHERE active"):
            k = boards.kind(r["listings_url"])
            by_kind[k] = by_kind.get(k, 0) + 1
        last_ingest = db.query(
            """
            SELECT status, COUNT(*) AS n FROM (
                SELECT DISTINCT ON (payload->>'source') status
                FROM tasks WHERE kind = 'ingest_source'
                ORDER BY payload->>'source', id DESC
            ) t GROUP BY status
            """
        )
        return CatalogCounts(
            sources=total["sources"] if total else 0,
            active=total["active"] if total else 0,
            by_kind=by_kind,
            last_ingest={r["status"]: r["n"] for r in last_ingest},
            bundles=(db.query_one("SELECT COUNT(*) AS n FROM source_groups") or {}).get("n", 0),
        )

    # Each fact is aggregated once over its table and joined, rather than
    # computed per source in a correlated subquery. At 751 sources the
    # per-row form ran five subplans and a lateral per row and took 890 ms
    # on production (EXPLAIN ANALYZE, 2026-09-04); this shape is one pass
    # over jobs, one over user_sources, one over source_groups and one
    # DISTINCT ON over the ingest tasks.
    rows = db.query(
        """
        WITH catalog AS (
            SELECT source, COUNT(*) AS jobs, MAX(created_at) AS last_new_posting_at
            FROM jobs GROUP BY source
        ),
        subscribers AS (
            SELECT source, COUNT(*) AS subscribers FROM user_sources GROUP BY source
        ),
        bundles AS (
            SELECT m AS source, array_agg(g.name ORDER BY g.name) AS groups
            FROM source_groups g, unnest(g.members) AS m GROUP BY m
        ),
        last_ingest AS (
            SELECT DISTINCT ON (payload->>'source')
                   payload->>'source' AS source, status, finished_at, error
            FROM tasks WHERE kind = 'ingest_source'
            ORDER BY payload->>'source', id DESC
        )
        -- title_pattern is not here: it is a 584-byte regex repeated on
        -- most rows, more than half of a 1.8 MB body at 1,732 sources, and
        -- only the edit form reads it, through GET /admin/sources/{name}.
        SELECT s.name, s.listings_url, s.description, s.active, s.created_at,
               s.company, s.ingest_interval_hours,
               -- Bundle membership per row, so grouping the list needs no
               -- client-side join of every row against every bundle.
               COALESCE(b.groups, '{}') AS groups,
               COALESCE(c.jobs, 0) AS jobs,
               COALESCE(u.subscribers, 0) AS subscribers,
               li.status AS last_ingest_status,
               li.finished_at AS last_ingest_at,
               li.error AS last_ingest_error,
               -- The number that retires a source, and the only one that
               -- can. last_ingest_at says the fetch worked; this says the
               -- fetch found anything we had not already seen. They
               -- diverge, and the gap IS the signal: fulltime_ouckah had
               -- 215 successful ingests and no new posting since the catalog
               -- was reseeded, reporting green every hour.
               c.last_new_posting_at
        FROM sources s
        LEFT JOIN catalog c ON c.source = s.name
        LEFT JOIN subscribers u ON u.source = s.name
        LEFT JOIN bundles b ON b.source = s.name
        LEFT JOIN last_ingest li ON li.source = s.name
        ORDER BY s.active DESC, s.name
        """
    )
    # The pull still queued or running per board, so the page disables its
    # button from server state and POST /admin/ingest's refusal is not a
    # surprise. The last_ingest CTE answers "how did the last one go";
    # this answers "is one going".
    in_flight = {
        r["source"]: Ingesting(id=r["id"], status=r["status"], created_at=r["created_at"])
        for r in db.query(
            """
            SELECT DISTINCT ON (payload->>'source') payload->>'source' AS source,
                   id, status, created_at
            FROM tasks WHERE kind = 'ingest_source'
              AND status IN ('pending', 'running', 'awaiting_batch', 'waiting')
            ORDER BY payload->>'source', id DESC
            """
        )
    }
    # The format is read off the URL, never stored, so it cannot drift from
    # what ingest will actually do with the row. This is the top-level
    # category the switch endpoint selects by.
    return SourceLedgers(
        sources=[
            SourceLedger(**r, kind=boards.kind(r["listings_url"]), task=in_flight.get(r["name"]))
            for r in rows
        ]
    )


_SOURCE_COLS = (
    "name, listings_url, description, active, created_at, company, title_pattern, "
    "ingest_interval_hours"
)


class SourceBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    listings_url: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=500)
    active: bool | None = None
    company: str | None = Field(default=None, max_length=200)
    title_pattern: str | None = Field(default=None, max_length=500)
    # Hours between pulls; 1 is the hourly cycle. Bounded above by a week so a
    # typo cannot park a board for a year while it reads as active.
    ingest_interval_hours: int | None = Field(default=None, ge=1, le=168)


def _check_source(listings_url: str, company: str | None, title_pattern: str | None) -> None:
    """The two facts a source row can get wrong silently: a company board on a
    system that never names the company, and a pattern that ingest cannot
    compile. Both would surface only as a failed ingest an hour later."""
    if boards.kind(listings_url) in boards.NEEDS_COMPANY and not (company or "").strip():
        raise HTTPException(
            400,
            detail={
                "code": "COMPANY_REQUIRED",
                "message": f"a {boards.kind(listings_url)} board never names its company; "
                "set company to the employer it belongs to",
            },
        )
    if title_pattern:
        try:
            re.compile(title_pattern, re.IGNORECASE)
        except re.error as exc:
            raise HTTPException(
                400,
                detail={"code": "BAD_TITLE_PATTERN", "message": f"title_pattern: {exc}"},
            ) from exc


@router.post("/sources")
def create_source(body: SourceBody, user: AuthedUser = Depends(require_admin)) -> CatalogSource:
    if not body.name or not body.listings_url:
        raise HTTPException(
            400, detail={"code": "MISSING_FIELDS", "message": "name and listings_url are required"}
        )
    _check_source(body.listings_url, body.company, body.title_pattern)
    if db.query_one("SELECT name FROM sources WHERE name = %s", (body.name,)):
        raise HTTPException(409, detail={"code": "DUPLICATE_NAME", "message": "source name exists"})
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
    src = db.query_one("SELECT name FROM sources WHERE name = %s", (name,))
    if not src:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    attached = db.query_one(
        """
        SELECT (SELECT count(*) FROM jobs WHERE source = %(n)s) AS jobs,
               (SELECT count(*) FROM user_sources WHERE source = %(n)s) AS subscribers,
               (SELECT count(*) FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
                WHERE j.source = %(n)s) AS board_rows
        """,
        {"n": name},
    )
    # The aggregate always returns exactly one row, but assert it rather than
    # subscript an Optional - a silent None here would 500 mid-delete.
    assert attached is not None
    if not force and (attached["jobs"] or attached["subscribers"] or attached["board_rows"]):
        raise HTTPException(
            409,
            detail={
                "code": "SOURCE_IN_USE",
                "message": (
                    f"{name} still has {attached['jobs']} jobs, "
                    f"{attached['subscribers']} subscribers, {attached['board_rows']} board rows"
                ),
                "attached": attached,
            },
        )
    db.execute("DELETE FROM user_sources WHERE source = %s", (name,))
    db.execute(
        "UPDATE source_groups SET members = array_remove(members, %s) WHERE %s = ANY(members)",
        (name, name),
    )
    db.execute("DELETE FROM sources WHERE name = %s", (name,))
    return SourceDeleted(ok=True, deleted=name, was_attached=Attached(**attached))


@router.get("/sources/{name}")
def get_source(name: str, user: AuthedUser = Depends(require_admin)) -> CatalogSource:
    """One source as stored, title_pattern included; the list shape omits it."""
    row = db.query_one(f"SELECT {_SOURCE_COLS} FROM sources WHERE name = %s", (name,))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    return CatalogSource(**row, kind=boards.kind(row["listings_url"]))


@router.patch("/sources/{name}")
def patch_source(
    name: str, body: SourceBody, user: AuthedUser = Depends(require_admin)
) -> CatalogSource:
    fields = body.model_dump(exclude_unset=True, exclude={"name"})
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    for k in ("company", "title_pattern"):
        if k in fields:
            fields[k] = (fields[k] or "").strip() or None
    current = db.query_one(f"SELECT {_SOURCE_COLS} FROM sources WHERE name = %s", (name,))
    if not current:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    merged = {**current, **fields}
    _check_source(merged["listings_url"], merged["company"], merged["title_pattern"])
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    row = db.query_one(
        f"UPDATE sources SET {cols} WHERE name = %(name)s RETURNING {_SOURCE_COLS}",
        {"name": name, **fields},
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    return CatalogSource(**row, kind=boards.kind(row["listings_url"]))


class SourceSwitchBody(BaseModel):
    # What to set on the selection: the on/off flag, the pull interval, or
    # both. At least one.
    active: bool | None = None
    ingest_interval_hours: int | None = Field(default=None, ge=1, le=168)
    # Any combination; the selection is their union. A kind is the board format
    # read off the listings URL (core/boards.kind), a group is a source bundle.
    kind: str | None = None
    group: str | None = None
    names: list[str] | None = None


@router.post("/sources/switch")
def switch_sources(
    body: SourceSwitchBody, user: AuthedUser = Depends(require_admin)
) -> SourcesSwitched:
    """One write sets a whole category of boards: on or off, and how often
    they are pulled.

    sources.active is already the switch that stops both the scrape and the AI
    spend on a board's postings (SUBSCRIBED_SOURCE in core/store.py), and
    ingest_interval_hours is what the scheduler reads, so a category is a way
    of SELECTING rows for those writes, not a second layer of state. Every row
    shows its own values afterwards, and nothing is overridden silently. The
    top level is the format (all Workday boards), the level below is a bundle
    (the quant firms), and names catch the rest.
    """
    if body.kind is None and body.group is None and not body.names:
        raise HTTPException(
            400, detail={"code": "NO_SELECTION", "message": "give a kind, a group, or names"}
        )
    if body.active is None and body.ingest_interval_hours is None:
        raise HTTPException(
            400,
            detail={
                "code": "NOTHING_TO_SET",
                "message": "give active, ingest_interval_hours, or both",
            },
        )
    selected: set[str] = set(body.names or [])
    if body.group is not None:
        grp = db.query_one("SELECT members FROM source_groups WHERE name = %s", (body.group,))
        if not grp:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown group"})
        selected |= set(grp["members"])
    if body.kind is not None:
        selected |= {
            r["name"]
            for r in db.query("SELECT name, listings_url FROM sources")
            if boards.kind(r["listings_url"]) == body.kind
        }
    sets = {
        k: v
        for k, v in (("active", body.active), ("ingest_interval_hours", body.ingest_interval_hours))
        if v is not None
    }
    changed = db.query(
        "UPDATE sources SET "
        + ", ".join(f"{k} = %({k})s" for k in sets)
        + " WHERE name = ANY(%(names)s) AND ("
        + " OR ".join(f"{k} IS DISTINCT FROM %({k})s" for k in sets)
        + ") RETURNING name",
        {**sets, "names": sorted(selected)},
    )
    return SourcesSwitched(
        active=body.active,
        ingest_interval_hours=body.ingest_interval_hours,
        selected=sorted(selected),
        changed=sorted(r["name"] for r in changed),
    )


class PatternPreviewBody(BaseModel):
    title_pattern: str = Field(max_length=500)
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
    if not db.query_one("SELECT 1 FROM sources WHERE name = %s", (name,)):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    try:
        candidate = re.compile(body.title_pattern, re.IGNORECASE)
    except re.error as exc:
        raise HTTPException(
            400, detail={"code": "BAD_TITLE_PATTERN", "message": f"title_pattern: {exc}"}
        ) from exc
    titles = db.query(
        """
        SELECT url, title, CASE WHEN kept THEN 'catalog' ELSE 'screened' END AS held_in
        FROM listings WHERE source = %(name)s
        ORDER BY title
        """,
        {"name": name},
    )
    admitted = [t for t in titles if candidate.search(t["title"])]
    excluded = [t for t in titles if not candidate.search(t["title"])]
    # The catalog side is what the LIVE pattern admitted; a candidate that
    # excludes some of it is narrowing, one that admits screened rows is
    # widening. Both counts, so the change reads as what it is.
    return PatternPreview(
        source=name,
        title_pattern=body.title_pattern,
        titles=len(titles),
        admitted=len(admitted),
        excluded=len(excluded),
        would_add=sum(1 for t in admitted if t["held_in"] == "screened"),
        would_drop=sum(1 for t in excluded if t["held_in"] == "catalog"),
        samples=PatternSamples(
            admitted=[t["title"] for t in admitted[: body.samples]],
            excluded=[t["title"] for t in excluded[: body.samples]],
        ),
    )


@router.get("/sources/{name}/screened")
def screened_postings(
    name: str, limit: int = 100, offset: int = 0, user: AuthedUser = Depends(require_admin)
) -> ScreenedPostings:
    """The postings this board lists that its title pattern did not admit,
    newest listing first, so an admin can see what a pattern is costing."""
    limit = max(1, min(limit, 500))
    total = db.query_one(
        "SELECT count(*) AS n FROM listings WHERE source = %s AND NOT kept", (name,)
    )
    rows = db.query_as(
        ScreenedPosting,
        "SELECT url, company, title, locations, date_posted, pattern, first_seen_at, last_seen_at "
        "FROM listings WHERE source = %s AND NOT kept "
        "ORDER BY date_posted DESC NULLS LAST, title LIMIT %s OFFSET %s",
        (name, limit, max(0, offset)),
    )
    n = total["n"] if total else 0
    return ScreenedPostings(source=name, rows=rows, total=n, has_more=offset + len(rows) < n)
