"""The boards: what people ask for, what each pull delivered, how the
sources are bundled, and the pace each egress address keeps."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import db, hosts, scoping, task_admission
from api import params as params_
from api.auth import AuthedUser
from api.routers.admin.shared import SUMMARY_MAX_HOURS, require_admin

router = APIRouter()


@router.get("/source-requests")
def list_source_requests(
    status: str = "open",
    users: str | None = Query(default=None, alias="user"),
    limit: int = 50,
    offset: int = 0,
    user: AuthedUser = Depends(require_admin),
):
    limit = max(1, min(limit, 200))
    statuses = [] if status.strip() == "all" else params_.csv(status)
    ids = scoping.user_ids(users)
    clauses = ["sr.status = ANY(%(status)s)"] if statuses else []
    if ids:
        clauses.append(scoping.column("sr.user_id"))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    selection = {"status": statuses, "user_ids": ids}
    rows = db.query(
        f"""
        SELECT sr.*, u.email AS requester_email, u.name AS requester_name
        FROM source_requests sr JOIN users u ON u.id = sr.user_id
        {where} ORDER BY sr.id DESC LIMIT %(limit)s OFFSET %(offset)s
        """,
        {**selection, "limit": limit + 1, "offset": max(0, offset)},
    )
    # The Requests badge once showed the page size as the queue count. The
    # catalog had 389 sources at that audit; a request queue can exceed one
    # page, so count the full selection independently of pagination.
    total = db.query_one(f"SELECT count(*) AS n FROM source_requests sr {where}", selection)
    return {
        "filters": params_.applied(status=statuses, user=scoping.echo(ids)),
        "filterable": ["status", "user"],
        "rows": rows[:limit],
        "has_more": len(rows) > limit,
        "total": total["n"] if total else 0,
    }


class ResolveSourceRequest(BaseModel):
    action: str
    note: str = ""


@router.post("/source-requests/{request_id}/resolve")
def resolve_source_request(
    request_id: int, body: ResolveSourceRequest, user: AuthedUser = Depends(require_admin)
):
    if body.action not in ("added", "dismissed"):
        raise HTTPException(
            400, detail={"code": "INVALID_ACTION", "message": "action must be added or dismissed"}
        )
    row = db.query_one(
        "UPDATE source_requests SET status = %s, resolution_note = %s, resolved_at = now() "
        "WHERE id = %s RETURNING id, status",
        (body.action, body.note[:2000] or None, request_id),
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown request"})
    return row


@router.get("/ingest")
def ingest_summary(hours: int = 24, user: AuthedUser = Depends(require_admin)):
    """What the boards delivered: per source over the window, from the counts
    each ingest leaves on its task (fetched, kept by the title pattern, pages
    cached, fetches that failed, postings the board reports gone) and the
    catalog rows that were new. A source that pulls fine and delivers nothing
    is visible here, which last_new_posting_at alone cannot show for a
    mirror."""
    hours = max(1, min(hours, SUMMARY_MAX_HOURS))
    # Same shape as the sources list: one pass per table, joined, instead of
    # a subquery per source (642 ms on production at 751 sources before).
    rows = db.query(
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
    totals = {
        k: sum(r[k] or 0 for r in rows)
        for k in (
            "pulls",
            "failed_pulls",
            "fetched",
            "kept",
            "cached",
            "fetch_failed",
            "gone",
            "new_jobs",
        )
    }
    return {"hours": hours, "rows": rows, "totals": totals}


class IngestBody(BaseModel):
    sources: list[str] | None = None


@router.post("/ingest")
def trigger_ingest(body: IngestBody, user: AuthedUser = Depends(require_admin)):
    """Queue an off-cycle pull for each active source without overlapping work."""
    active = {r["name"] for r in db.query("SELECT name FROM sources WHERE active")}
    wanted = list(dict.fromkeys(body.sources)) if body.sources else sorted(active)
    unknown = [s for s in wanted if s not in active]
    if unknown:
        raise HTTPException(
            400, detail={"code": "UNKNOWN_SOURCE", "message": f"unknown or inactive: {unknown}"}
        )
    cycle = f"manual-{user.id}-{int(time.time())}"
    task_ids = []
    in_flight = []
    for name in wanted:
        admission = task_admission.enqueue("ingest_source", {"source": name}, {"cycle": cycle})
        if admission.conflict:
            in_flight.append({"source": name, "task_id": admission.conflict.id})
        else:
            task_ids.append({"source": name, "task_id": admission.task_id})
    in_flight.sort(key=lambda row: row["source"])
    if in_flight and not task_ids:
        raise HTTPException(
            409,
            detail={
                "code": "IN_PROGRESS",
                "message": "every board named is already being pulled",
                "in_flight": in_flight,
            },
        )
    return {"tasks": task_ids, "in_flight": in_flight}


class SourceGroupBody(BaseModel):
    members: list[str] | None = None
    description: str | None = Field(default=None, max_length=500)
    active: bool | None = None


@router.post("/source-groups/{name}")
def upsert_source_group(
    name: str, body: SourceGroupBody, user: AuthedUser = Depends(require_admin)
):
    if body.members is not None:
        known = {r["name"] for r in db.query("SELECT name FROM sources")}
        unknown = [m for m in body.members if m not in known]
        if unknown:
            raise HTTPException(
                400, detail={"code": "UNKNOWN_SOURCE", "message": f"unknown sources: {unknown}"}
            )
    row = db.query_one(
        """
        INSERT INTO source_groups (name, members, description, active)
        VALUES (%(name)s, COALESCE(%(members)s, '{}'), COALESCE(%(description)s, ''),
                COALESCE(%(active)s, TRUE))
        ON CONFLICT (name) DO UPDATE SET
            members = COALESCE(%(members)s, source_groups.members),
            description = COALESCE(%(description)s, source_groups.description),
            active = COALESCE(%(active)s, source_groups.active)
        RETURNING *
        """,
        {
            "name": name,
            "members": body.members,
            "description": body.description,
            "active": body.active,
        },
    )
    return row


@router.delete("/source-groups/{name}")
def delete_source_group(name: str, user: AuthedUser = Depends(require_admin)):
    row = db.query_one("DELETE FROM source_groups WHERE name = %s RETURNING name", (name,))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown group"})
    return {"ok": True, "deleted": name}


@router.get("/host-budgets")
def host_budgets(user: AuthedUser = Depends(require_admin)):
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
    for r in rows:
        # Refused with nothing ever accepted is a block, not a pace; the page
        # says so instead of showing a gap that only grows.
        r["blocked"] = hosts.blocked(r["ok"], r["refused"])
    # The pulls waiting on a slot, per host, so the page need not page every
    # pending ingest task to say "3 waiting, next 14:52".
    deferred = db.query(
        """
        SELECT payload->>'host' AS host, COUNT(*) AS count, MIN(not_before) AS soonest_not_before
        FROM tasks
        WHERE kind = 'ingest_source' AND status = 'pending' AND not_before > now()
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    return {"budgets": rows, "deferred": deferred}
