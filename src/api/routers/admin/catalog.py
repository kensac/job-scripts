"""A posting and the corrections a person makes to one: the classified
location strings, what people reported as wrong, and the fixes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import db, pagination, scoping, task_admission
from api import params as params_
from api.auth import AuthedUser
from api.routers.admin.shared import require_admin
from api.routers.jobs import report_kinds

router = APIRouter()


class LocationPlace(BaseModel):
    country: str = Field(max_length=2)
    region: str | None = Field(default=None, max_length=2)
    city: str | None = None


class LocationPut(BaseModel):
    """One place through country/region/city, or several through places;
    places wins when both are sent."""

    country: str | None = Field(default=None, max_length=2)
    region: str | None = Field(default=None, max_length=2)
    city: str | None = None
    remote: bool = False
    places: list[LocationPlace] | None = Field(default=None, max_length=20)


@router.get("/locations")
def list_locations(
    q: str | None = None,
    unplaced: bool = False,
    limit: int = 200,
    offset: int = 0,
    user: AuthedUser = Depends(require_admin),
):
    """The classified location strings, most recent first, paged; q searches
    the text, unplaced narrows to strings the model could not place. total
    counts what the filters keep, total_all the whole table."""
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    where = ["true"]
    params: dict[str, Any] = {"limit": limit + 1, "offset": offset}
    if q:
        where.append("text ILIKE %(q)s")
        params["q"] = f"%{q}%"
    if unplaced:
        where.append("country IS NULL AND NOT remote")
    clause = " AND ".join(where)
    rows = db.query(
        f"SELECT text, country, region, city, remote, places, model, classified_at "
        f"FROM locations WHERE {clause} ORDER BY classified_at DESC, text "
        f"LIMIT %(limit)s OFFSET %(offset)s",
        params,
    )
    total = db.query_one(f"SELECT COUNT(*) AS c FROM locations WHERE {clause}", params)
    total_all = db.query_one("SELECT COUNT(*) AS c FROM locations")
    return {
        "rows": rows[:limit],
        "has_more": len(rows) > limit,
        "offset": offset,
        "total": total["c"] if total else 0,
        "total_all": total_all["c"] if total_all else 0,
        "filters": params_.applied(q=[q] if q else [], unplaced=["true"] if unplaced else []),
        "filterable": ["q", "unplaced"],
    }


@router.put("/locations/{text}")
def put_location(text: str, body: LocationPut, user: AuthedUser = Depends(require_admin)):
    """A person's classification of one string, kept over the model's: the
    sweep never re-asks about a string that has a row."""
    from tasks.locations import LocationExtract, Place, store

    store(
        text.strip(),
        LocationExtract(
            country=body.country or "",
            region=body.region or "",
            city=body.city or "",
            remote=body.remote,
            places=[
                Place(country=p.country, region=p.region or "", city=p.city or "")
                for p in (body.places or [])
            ],
        ),
        model="admin",
    )
    return db.query_one(
        "SELECT text, country, region, city, remote, places, model FROM locations WHERE text = %s",
        (text.strip(),),
    )


@router.get("/reports")
def list_reports(
    status: str = "open",
    users: str | None = Query(default=None, alias="user"),
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
):
    paging = pagination.Page.from_params(page, page_size, maximum=200)
    ids = scoping.user_ids(users)
    statuses = [] if status.strip() == "all" else params_.csv(status)
    clauses = ["r.status = ANY(%(status)s)"] if statuses else []
    if ids:
        clauses.append(scoping.column("r.user_id"))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total_row = db.query_one(
        f"SELECT COUNT(*) AS c FROM reports r {where}", {"status": statuses, "user_ids": ids}
    )
    rows = db.query(
        f"""
        SELECT r.*, u.email AS reporter_email, u.name AS reporter_name,
               j.url, j.company, j.title, j.source, j.extraction_status,
               COALESCE(c.status = 'rejected', FALSE) AS posting_closed
        FROM reports r
        JOIN users u ON u.id = r.user_id
        JOIN jobs j ON j.id = r.job_id
        LEFT JOIN LATERAL (
            SELECT status FROM ai_queries
            WHERE url = j.url AND check_type = 'closed' AND status IN ('passed', 'rejected')
            ORDER BY id DESC LIMIT 1
        ) c ON TRUE
        {where}
        ORDER BY r.id DESC LIMIT %(limit)s OFFSET %(offset)s
        """,
        {
            "status": statuses,
            "user_ids": ids,
            "limit": paging.size,
            "offset": paging.offset,
        },
    )
    total = total_row["c"] if total_row else 0
    return {
        "rows": rows,
        **paging.metadata(total),
        "report_kinds": report_kinds(),
        # The drawer offers "close this posting" only on a build that has it.
        "can_close_posting": True,
        "filters": params_.applied(status=statuses, user=scoping.echo(ids)),
        "filterable": ["status", "user"],
    }


class ResolveReport(BaseModel):
    action: str
    note: str = ""


@router.post("/reports/{report_id}/resolve")
def resolve_report(report_id: int, body: ResolveReport, user: AuthedUser = Depends(require_admin)):
    if body.action not in ("resolved", "dismissed"):
        raise HTTPException(
            400,
            detail={"code": "INVALID_ACTION", "message": "action must be resolved or dismissed"},
        )
    row = db.query_one(
        "UPDATE reports SET status = %s, resolution_note = %s, resolved_at = now() "
        "WHERE id = %s RETURNING id, status",
        (body.action, body.note[:2000] or None, report_id),
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown report"})
    return row


class JobCorrection(BaseModel):
    company: str | None = None
    title: str | None = None
    locations: list[str] | None = None
    terms: list[str] | None = None
    active: bool | None = None


@router.patch("/jobs/{job_id}")
def patch_catalog_job(job_id: int, body: JobCorrection, user: AuthedUser = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    row = db.query_one(
        f"UPDATE jobs SET {cols} WHERE id = %(jid)s "
        "RETURNING id, url, company, title, locations, terms, active",
        {"jid": job_id, **fields},
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    return row


class ClosePostingBody(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/jobs/{job_id}/close")
def close_posting(job_id: int, body: ClosePostingBody, user: AuthedUser = Depends(require_admin)):
    """An admin asserts the posting is closed, for everyone.

    The same mechanism the closed check uses: a verdict row, latest per url,
    that every board reads at visibility time. So the posting leaves every
    board on the next read, nothing re-runs, and the row records who said so
    and why. Not `active`: that is the catalog's fact about whether the board
    still lists it, and a board can keep listing a posting that should never
    have passed.
    """
    from api.ai import verdicts as _verdicts

    job = db.query_one("SELECT url, company, title FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    reason = f"closed by admin {user.email}" + (
        f": {body.reason.strip()}" if body.reason.strip() else ""
    )
    _verdicts.record_manual(
        url=job["url"],
        check_type="closed",
        rejected=True,
        reason=reason,
        company=job["company"] or "",
        job_title=job["title"] or "",
        context="admin",
    )
    return {"job_id": job_id, "url": job["url"], "posting_closed": True, "reason": reason}


@router.post("/jobs/{job_id}/reparse")
def reparse_job(job_id: int, user: AuthedUser = Depends(require_admin)):
    job = db.query_one("SELECT id FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    admission = task_admission.enqueue(
        "extract_upload",
        {"job_id": job_id},
        {"user_id": user.id, "force": True},
    )
    if admission.conflict:
        raise HTTPException(
            409,
            detail={
                "code": "IN_PROGRESS",
                "message": "this posting is already being parsed",
                "task_id": admission.conflict.id,
            },
        )
    return {"task_id": admission.task_id}
