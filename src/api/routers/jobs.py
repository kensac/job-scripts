from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.models import UploadRequest, UserJobPatch
from core.urls import normalize_url

router = APIRouter()

_JOB_ROW = """
    j.id AS job_id, j.company, j.title, j.locations, j.terms, j.source,
    j.url, j.raw_url, j.active, j.date_posted, j.created_at AS added_at,
    j.extraction_status,
    uj.status, uj.date_applied, uj.notes, uj.size, uj.recruiter,
    uj.connection1, uj.connection2, uj.documents,
    COALESCE(uj.hidden, FALSE) AS hidden
"""

_VISIBILITY = """
WITH enabled_filters AS (
    SELECT prompt_hash FROM user_filters WHERE user_id = %(uid)s AND enabled
),
latest_check AS (
    SELECT DISTINCT ON (url, check_type) url, check_type, status
    FROM ai_queries
    WHERE check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')
    ORDER BY url, check_type, id DESC
),
filter_pass AS (
    SELECT url, COUNT(*) AS passed_count FROM (
        SELECT DISTINCT ON (q.url, q.prompt_hash) q.url, q.status
        FROM ai_queries q
        JOIN enabled_filters f ON q.prompt_hash = f.prompt_hash
        WHERE q.check_type = 'custom' AND q.status IN ('passed', 'rejected')
        ORDER BY q.url, q.prompt_hash, q.id DESC
    ) t WHERE t.status = 'passed' GROUP BY url
)
SELECT {columns}
FROM jobs j
LEFT JOIN user_jobs uj ON uj.job_id = j.id AND uj.user_id = %(uid)s
WHERE (
    j.uploaded_by = %(uid)s
    OR uj.user_id IS NOT NULL
    OR (
        j.active
        AND j.source IN (SELECT source FROM user_sources WHERE user_id = %(uid)s)
        AND EXISTS (SELECT 1 FROM latest_check lc
                    WHERE lc.url = j.url AND lc.check_type = 'closed' AND lc.status = 'passed')
        AND (%(bypass_sponsorship)s
             OR EXISTS (SELECT 1 FROM latest_check lc
                        WHERE lc.url = j.url AND lc.check_type = 'clearance' AND lc.status = 'passed'))
        AND ((SELECT COUNT(*) FROM enabled_filters) = 0
             OR COALESCE((SELECT fp.passed_count FROM filter_pass fp WHERE fp.url = j.url), 0)
                = (SELECT COUNT(*) FROM enabled_filters))
    )
)
{extra}
"""


@router.get("/user/jobs")
def list_jobs(
    limit: int = 200,
    cursor: Optional[int] = None,
    search: Optional[str] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    include_hidden: bool = False,
    user: AuthedUser = Depends(require_user),
):
    limit = max(1, min(limit, 1000))
    bypass = db.query_one(
        "SELECT bypass_sponsorship_filter FROM user_settings WHERE user_id = %s",
        (user.id,),
    )
    extra = []
    params: dict = {
        "uid": user.id,
        "limit": limit + 1,
        "bypass_sponsorship": bool(bypass and bypass["bypass_sponsorship_filter"]),
    }
    if not include_hidden:
        extra.append("AND COALESCE(uj.hidden, FALSE) = FALSE")
    if cursor is not None:
        extra.append("AND j.id < %(cursor)s")
        params["cursor"] = cursor
    if search:
        extra.append("AND (j.company ILIKE %(search)s OR j.title ILIKE %(search)s)")
        params["search"] = f"%{search}%"
    if status:
        extra.append("AND uj.status = %(status)s")
        params["status"] = status
    if source:
        extra.append("AND j.source = %(source)s")
        params["source"] = source
    extra.append("ORDER BY j.id DESC LIMIT %(limit)s")
    sql = _VISIBILITY.format(columns=_JOB_ROW, extra="\n".join(extra))
    rows = db.query(sql, params)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "rows": rows,
        "next_cursor": rows[-1]["job_id"] if has_more and rows else None,
    }


@router.patch("/user/jobs/{job_id}")
def patch_job(job_id: int, body: UserJobPatch, user: AuthedUser = Depends(require_user)):
    if not db.query_one("SELECT id FROM jobs WHERE id = %s", (job_id,)):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    insert_cols = ", ".join(fields)
    insert_vals = ", ".join(f"%({k})s" for k in fields)
    db.execute(
        f"""
        INSERT INTO user_jobs (user_id, job_id, {insert_cols})
        VALUES (%(uid)s, %(jid)s, {insert_vals})
        ON CONFLICT (user_id, job_id) DO UPDATE SET {cols}, updated_at = now()
        """,
        {"uid": user.id, "jid": job_id, **fields},
    )
    return {"ok": True}


@router.post("/uploads")
def upload_links(body: UploadRequest, user: AuthedUser = Depends(require_user)):
    accepted = []
    for raw in body.urls:
        raw = raw.strip()
        if not raw.startswith(("http://", "https://")):
            continue
        url = normalize_url(raw)
        row = db.query_one(
            """
            INSERT INTO jobs (url, raw_url, source, uploaded_by, extraction_status)
            VALUES (%s, %s, 'upload', %s, 'pending')
            ON CONFLICT (url) DO UPDATE SET
                extraction_status = CASE WHEN jobs.extraction_status = 'failed'
                                         THEN 'pending' ELSE jobs.extraction_status END
            RETURNING id, extraction_status
            """,
            (url, raw, user.id),
        )
        assert row is not None
        db.execute(
            "INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user.id, row["id"]),
        )
        if row["extraction_status"] == "pending":
            db.execute(
                "INSERT INTO tasks (kind, payload) VALUES ('extract_upload', %s)",
                (db.jsonb({"job_id": row["id"], "user_id": user.id}),),
            )
        accepted.append({"job_id": row["id"], "url": url})
    return {"accepted": accepted}


REPORT_KINDS = ("stale", "wrong_data", "closed", "other")


class ReportBody(BaseModel):
    kind: str
    message: str = ""
    corrections: Optional[dict] = None


@router.post("/user/jobs/{job_id}/report")
def report_job(job_id: int, body: ReportBody, user: AuthedUser = Depends(require_user)):
    if body.kind not in REPORT_KINDS:
        raise HTTPException(
            400,
            detail={"code": "INVALID_KIND", "message": f"kind must be one of {REPORT_KINDS}"},
        )
    if not db.query_one("SELECT id FROM jobs WHERE id = %s", (job_id,)):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    row = db.query_one(
        """
        INSERT INTO reports (user_id, job_id, kind, message, corrections)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, status, created_at
        """,
        (
            user.id,
            job_id,
            body.kind,
            body.message[:2000],
            db.jsonb(body.corrections) if body.corrections is not None else None,
        ),
    )
    return row


@router.get("/tasks/{task_id}")
def get_task(task_id: int, user: AuthedUser = Depends(require_user)):
    row = db.query_one(
        "SELECT id, kind, status, progress, error, created_at, started_at, finished_at "
        "FROM tasks WHERE id = %s",
        (task_id,),
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown task"})
    return row
