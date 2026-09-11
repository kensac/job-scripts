"""The boards: what people ask for, what each pull delivered, how the
sources are bundled, and the pace each egress address keeps."""

from __future__ import annotations

import datetime
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import db, hosts, scoping, task_admission
from api import params as params_
from api.auth import AuthedUser
from api.routers.admin.shared import SUMMARY_MAX_HOURS, require_admin

router = APIRouter()


class RequestedSource(BaseModel):
    """A board somebody asked for, with who asked. `status` is open, added or
    dismissed, and a resolved request keeps its row so the same board is not
    added twice."""

    id: int
    user_id: int
    url: str
    note: str
    status: str
    resolution_note: str | None
    created_at: datetime.datetime
    resolved_at: datetime.datetime | None
    requester_email: str | None
    requester_name: str | None


class RequestedSources(BaseModel):
    """`total` counts the whole selection, not the page. The Requests badge
    once showed the page size as the queue count."""

    filters: dict[str, list[str]]
    filterable: list[str]
    rows: list[RequestedSource]
    has_more: bool
    total: int


@router.get("/source-requests")
def list_source_requests(
    status: str = "open",
    users: str | None = Query(default=None, alias="user"),
    limit: int = 50,
    offset: int = 0,
    user: AuthedUser = Depends(require_admin),
) -> RequestedSources:
    limit = max(1, min(limit, 200))
    statuses = [] if status.strip() == "all" else params_.csv(status)
    ids = scoping.user_ids(users)
    clauses = ["sr.status = ANY(%(status)s)"] if statuses else []
    if ids:
        clauses.append(scoping.column("sr.user_id"))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    selection = {"status": statuses, "user_ids": ids}
    rows = db.query_as(
        RequestedSource,
        f"""
        SELECT sr.id, sr.user_id, sr.url, sr.note, sr.status, sr.resolution_note,
               sr.created_at, sr.resolved_at,
               u.email AS requester_email, u.name AS requester_name
        FROM source_requests sr JOIN users u ON u.id = sr.user_id
        {where} ORDER BY sr.id DESC LIMIT %(limit)s OFFSET %(offset)s
        """,
        {**selection, "limit": limit + 1, "offset": max(0, offset)},
    )
    # The Requests badge once showed the page size as the queue count. The
    # catalog had 389 sources at that audit; a request queue can exceed one
    # page, so count the full selection independently of pagination.
    total = db.query_one(f"SELECT count(*) AS n FROM source_requests sr {where}", selection)
    return RequestedSources(
        filters=params_.applied(status=statuses, user=scoping.echo(ids)),
        filterable=["status", "user"],
        rows=rows[:limit],
        has_more=len(rows) > limit,
        total=total["n"] if total else 0,
    )


class ResolveSourceRequest(BaseModel):
    action: str
    note: str = ""


class SourceRequestResolved(BaseModel):
    id: int
    status: str


@router.post("/source-requests/{request_id}/resolve")
def resolve_source_request(
    request_id: int, body: ResolveSourceRequest, user: AuthedUser = Depends(require_admin)
) -> SourceRequestResolved:
    if body.action not in ("added", "dismissed"):
        raise HTTPException(
            400, detail={"code": "INVALID_ACTION", "message": "action must be added or dismissed"}
        )
    row = db.query_one_as(
        SourceRequestResolved,
        "UPDATE source_requests SET status = %s, resolution_note = %s, resolved_at = now() "
        "WHERE id = %s RETURNING id, status",
        (body.action, body.note[:2000] or None, request_id),
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown request"})
    return row


class SourceIngest(BaseModel):
    """One board over the window. The counts come from what each ingest task
    left on its progress: `fetched` is what the board listed, `kept` is what
    the title pattern admitted, `cached` is pages we already held, `gone` is
    postings the board reports removed. `new_jobs` is catalog rows created,
    which is the only one of these that is not a task's own account of itself.

    A source that pulls fine and delivers nothing shows pulls with no
    new_jobs, which last_new_posting_at alone cannot show for a mirror.
    """

    name: str
    active: bool
    company: str | None
    ingest_interval_hours: int
    groups: list[str]
    pulls: int
    failed_pulls: int
    fetched: int
    kept: int
    cached: int
    fetch_failed: int
    gone: int
    last_pull_at: datetime.datetime | None
    new_jobs: int


class IngestTotals(BaseModel):
    """The same counts summed over every board, so the page need not add up
    751 rows to show a headline."""

    pulls: int
    failed_pulls: int
    fetched: int
    kept: int
    cached: int
    fetch_failed: int
    gone: int
    new_jobs: int


class IngestSummary(BaseModel):
    hours: int
    rows: list[SourceIngest]
    totals: IngestTotals


@router.get("/ingest")
def ingest_summary(hours: int = 24, user: AuthedUser = Depends(require_admin)) -> IngestSummary:
    """What the boards delivered: per source over the window, from the counts
    each ingest leaves on its task (fetched, kept by the title pattern, pages
    cached, fetches that failed, postings the board reports gone) and the
    catalog rows that were new. A source that pulls fine and delivers nothing
    is visible here, which last_new_posting_at alone cannot show for a
    mirror."""
    hours = max(1, min(hours, SUMMARY_MAX_HOURS))
    # Same shape as the sources list: one pass per table, joined, instead of
    # a subquery per source (642 ms on production at 751 sources before).
    rows = db.query_as(
        SourceIngest,
        """
        WITH pulls AS (
            SELECT payload->>'source' AS source,
                   COUNT(*) AS pulls,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed_pulls,
                   COALESCE(SUM((progress->>'fetched')::int), 0) AS fetched,
                   COALESCE(SUM((progress->>'kept')::int), 0) AS kept,
                   COALESCE(SUM((progress->>'cached')::int), 0) AS cached,
                   COALESCE(SUM((progress->>'fetch_failed')::int), 0) AS fetch_failed,
                   COALESCE(SUM((progress->>'gone')::int), 0) AS gone,
                   MAX(finished_at) AS last_pull_at
            FROM tasks
            WHERE kind = 'ingest_source' AND status IN ('done', 'failed')
              AND finished_at > now() - make_interval(hours => %(hours)s)
            GROUP BY 1
        ),
        fresh AS (
            SELECT source, COUNT(*) AS new_jobs FROM jobs
            WHERE created_at > now() - make_interval(hours => %(hours)s)
            GROUP BY source
        ),
        bundles AS (
            SELECT m AS source, array_agg(g.name ORDER BY g.name) AS groups
            FROM source_groups g, unnest(g.members) AS m GROUP BY m
        )
        SELECT s.name, s.active, s.company, s.ingest_interval_hours,
               COALESCE(b.groups, '{}') AS groups,
               COALESCE(p.pulls, 0) AS pulls,
               COALESCE(p.failed_pulls, 0) AS failed_pulls,
               COALESCE(p.fetched, 0) AS fetched,
               COALESCE(p.kept, 0) AS kept,
               COALESCE(p.cached, 0) AS cached,
               COALESCE(p.fetch_failed, 0) AS fetch_failed,
               COALESCE(p.gone, 0) AS gone,
               p.last_pull_at,
               COALESCE(f.new_jobs, 0) AS new_jobs
        FROM sources s
        LEFT JOIN pulls p ON p.source = s.name
        LEFT JOIN fresh f ON f.source = s.name
        LEFT JOIN bundles b ON b.source = s.name
        ORDER BY new_jobs DESC, s.name
        """,
        {"hours": hours},
    )
    totals = IngestTotals(
        **{field: sum(getattr(r, field) or 0 for r in rows) for field in IngestTotals.model_fields}
    )
    return IngestSummary(hours=hours, rows=rows, totals=totals)


class IngestBody(BaseModel):
    sources: list[str] | None = None


class SourceTask(BaseModel):
    source: str
    task_id: int


class IngestQueued(BaseModel):
    """What was queued, and what was already being pulled. A board already in
    flight is reported rather than queued twice; the request only fails when
    every board named was in flight and nothing was queued at all."""

    tasks: list[SourceTask]
    in_flight: list[SourceTask]


@router.post("/ingest")
def trigger_ingest(body: IngestBody, user: AuthedUser = Depends(require_admin)) -> IngestQueued:
    """Queue an off-cycle pull for each active source without overlapping work."""
    active = {r["name"] for r in db.query("SELECT name FROM sources WHERE active")}
    wanted = list(dict.fromkeys(body.sources)) if body.sources else sorted(active)
    unknown = [s for s in wanted if s not in active]
    if unknown:
        raise HTTPException(
            400, detail={"code": "UNKNOWN_SOURCE", "message": f"unknown or inactive: {unknown}"}
        )
    cycle = f"manual-{user.id}-{int(time.time())}"
    task_ids: list[SourceTask] = []
    in_flight: list[SourceTask] = []
    for name in wanted:
        admission = task_admission.enqueue("ingest_source", {"source": name}, {"cycle": cycle})
        if admission.conflict:
            in_flight.append(SourceTask(source=name, task_id=admission.conflict.id))
        else:
            assert admission.task_id is not None
            task_ids.append(SourceTask(source=name, task_id=admission.task_id))
    in_flight.sort(key=lambda row: row.source)
    if in_flight and not task_ids:
        raise HTTPException(
            409,
            detail={
                "code": "IN_PROGRESS",
                "message": "every board named is already being pulled",
                "in_flight": [row.model_dump() for row in in_flight],
            },
        )
    return IngestQueued(tasks=task_ids, in_flight=in_flight)


class SourceGroupBody(BaseModel):
    members: list[str] | None = None
    description: str | None = Field(default=None, max_length=500)
    active: bool | None = None


class SourceBundle(BaseModel):
    """A named set of boards, so a person subscribes to "quant" rather than to
    forty sources one at a time. `members` are source names."""

    name: str
    members: list[str]
    description: str
    active: bool
    created_at: datetime.datetime


@router.post("/source-groups/{name}")
def upsert_source_group(
    name: str, body: SourceGroupBody, user: AuthedUser = Depends(require_admin)
) -> SourceBundle:
    if body.members is not None:
        known = {r["name"] for r in db.query("SELECT name FROM sources")}
        unknown = [m for m in body.members if m not in known]
        if unknown:
            raise HTTPException(
                400, detail={"code": "UNKNOWN_SOURCE", "message": f"unknown sources: {unknown}"}
            )
    row = db.query_one_as(
        SourceBundle,
        """
        INSERT INTO source_groups (name, members, description, active)
        VALUES (%(name)s, COALESCE(%(members)s, '{}'), COALESCE(%(description)s, ''),
                COALESCE(%(active)s, TRUE))
        ON CONFLICT (name) DO UPDATE SET
            members = COALESCE(%(members)s, source_groups.members),
            description = COALESCE(%(description)s, source_groups.description),
            active = COALESCE(%(active)s, source_groups.active)
        RETURNING name, members, description, active, created_at
        """,
        {
            "name": name,
            "members": body.members,
            "description": body.description,
            "active": body.active,
        },
    )
    # The upsert always writes a row, so there is one to return.
    assert row is not None
    return row


class BundleDeleted(BaseModel):
    ok: bool
    deleted: str


@router.delete("/source-groups/{name}")
def delete_source_group(name: str, user: AuthedUser = Depends(require_admin)) -> BundleDeleted:
    row = db.query_one("DELETE FROM source_groups WHERE name = %s RETURNING name", (name,))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown group"})
    return BundleDeleted(ok=True, deleted=name)


class HostBudget(BaseModel):
    """The pace one egress address keeps against one board host, as the fleet
    has learned it.

    `closed` means the next slot is in the future, which is ordinary pacing.
    `blocked` is the different thing: refused repeatedly with nothing ever
    accepted, so no gap will open it. It is computed on read rather than
    stored, so the rule lives in one place (api.hosts.blocked).
    """

    host: str
    egress_group: str
    pace_seconds: float
    next_allowed_at: datetime.datetime
    ok: int
    refused: int
    updated_at: datetime.datetime
    closed: bool
    blocked: bool


class DeferredPulls(BaseModel):
    """The pulls waiting on a slot for one host, so the page can say "3
    waiting, next 14:52" without paging every pending ingest task."""

    host: str | None
    count: int
    soonest_not_before: datetime.datetime | None


class HostBudgets(BaseModel):
    budgets: list[HostBudget]
    deferred: list[DeferredPulls]


@router.get("/host-budgets")
def host_budgets(user: AuthedUser = Depends(require_admin)) -> HostBudgets:
    """The pace each egress address keeps against each board host, as the
    fleet has learned it: a host that keeps refusing shows a growing gap and
    a rising refused count, and the address that is fine shows neither."""
    rows = db.query(
        """
        SELECT host, egress_group, pace_seconds, next_allowed_at, ok, refused, updated_at,
               next_allowed_at > now() AS closed
        FROM host_budget ORDER BY refused DESC, host, egress_group
        """
    )
    return HostBudgets(
        budgets=[HostBudget(**r, blocked=hosts.blocked(r["ok"], r["refused"])) for r in rows],
        deferred=db.query_as(
            DeferredPulls,
            """
            SELECT payload->>'host' AS host, COUNT(*) AS count,
                   MIN(not_before) AS soonest_not_before
            FROM tasks
            WHERE kind = 'ingest_source' AND status = 'pending' AND not_before > now()
            GROUP BY 1 ORDER BY 2 DESC
            """,
        ),
    )
