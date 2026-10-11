"""Admin source vocabulary, dashboard counts and full source ledger."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser
from api.routers.admin import require_admin
from core.fetching import boards

router = APIRouter()


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


@router.get("/sources")
def admin_list_sources(
    shape: str = "full", user: AuthedUser = Depends(require_admin)
) -> SourceNames | CatalogCounts | SourceLedgers:
    """shape=full is the ledger. shape=names is the vocabulary (name, active,
    kind, bundles) for pickers and filters; shape=counts is what a dashboard
    tile needs. Both skip the per-source aggregation over jobs, tasks and
    subscribers, which the dashboard was paying for on every live tick."""
    if shape == "names":
        source_rows = db.query(
            """
            WITH bundles AS (
                SELECT m AS source, array_agg(DISTINCT g.name ORDER BY g.name) AS groups
                FROM source_groups g, unnest(g.members) AS m GROUP BY m
            )
            SELECT s.name, s.listings_url, s.active, COALESCE(b.groups, '{}') AS groups
            FROM sources s LEFT JOIN bundles b ON b.source = s.name
            ORDER BY s.active DESC, s.name
            """
        )
        return SourceNames(
            sources=[
                SourceName(
                    name=source_row["name"],
                    active=source_row["active"],
                    groups=source_row["groups"],
                    kind=boards.kind(source_row["listings_url"]),
                )
                for source_row in source_rows
            ]
        )
    if shape == "counts":
        total = db.query_one(
            "SELECT COUNT(*) AS sources, COUNT(*) FILTER (WHERE active) AS active FROM sources"
        )
        by_kind: dict[str, int] = {}
        for source_row in db.query("SELECT listings_url FROM sources WHERE active"):
            source_kind = boards.kind(source_row["listings_url"])
            by_kind[source_kind] = by_kind.get(source_kind, 0) + 1
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
            last_ingest={status_row["status"]: status_row["n"] for status_row in last_ingest},
            bundles=(db.query_one("SELECT COUNT(*) AS n FROM source_groups") or {}).get("n", 0),
        )

    # Each fact is aggregated once over its table and joined, rather than
    # computed per source in a correlated subquery. At 751 sources the
    # per-row form ran five subplans and a lateral per row and took 890 ms
    # on production (EXPLAIN ANALYZE, 2026-09-04); this shape is one pass
    # over jobs, one over the people's sources and one over source_groups. The latest
    # ingest is the exception: an indexed LIMIT 1 per source is cheaper than
    # a pass over every ingest task. The DISTINCT ON it replaced sorted
    # 136,986 tasks in 0.59 s on production (2026-10-04); the probe took
    # 39 ms on a test copy at that scale.
    source_rows = db.query(
        """
        WITH catalog AS (
            SELECT source, COUNT(*) AS jobs, MAX(created_at) AS last_new_posting_at
            FROM jobs GROUP BY source
        ),
        subscribers AS (
            SELECT source, COUNT(*) AS subscribers FROM user_source_set GROUP BY source
        ),
        bundles AS (
            SELECT m AS source, array_agg(g.name ORDER BY g.name) AS groups
            FROM source_groups g, unnest(g.members) AS m GROUP BY m
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
        -- The latest ingest by id: one probe of idx_tasks_ingest_source_latest
        -- per source instead of a DISTINCT ON sorting every ingest task.
        LEFT JOIN LATERAL (
            SELECT status, finished_at, error FROM tasks
            WHERE kind = 'ingest_source' AND payload->>'source' = s.name
            ORDER BY id DESC LIMIT 1
        ) li ON true
        ORDER BY s.active DESC, s.name
        """
    )
    # The pull still queued or running per board, so the page disables its
    # button from server state and POST /admin/ingest's refusal is not a
    # surprise. The last_ingest CTE answers "how did the last one go";
    # this answers "is one going".
    ingest_tasks_in_flight = {
        task_row["source"]: Ingesting(
            id=task_row["id"], status=task_row["status"], created_at=task_row["created_at"]
        )
        for task_row in db.query(
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
            SourceLedger(
                **source_row,
                kind=boards.kind(source_row["listings_url"]),
                task=ingest_tasks_in_flight.get(source_row["name"]),
            )
            for source_row in source_rows
        ]
    )
