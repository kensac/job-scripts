from __future__ import annotations

import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import criteria, db, events
from api.auth import AuthedUser, require_user
from api.models import UploadRequest, UserJobPatch
from core.urls import normalize_url

router = APIRouter()

_JOB_ROW = """
    j.id AS job_id, j.company, j.title, j.locations, j.terms, j.source,
    j.url, j.raw_url, j.active, j.date_posted, j.created_at AS added_at,
    j.extraction_status, j.comp_min, j.comp_max, j.comp_text,
    uj.status, uj.date_applied, uj.notes, uj.size, uj.recruiter,
    uj.connection1, uj.connection2, uj.documents,
    COALESCE(uj.hidden, FALSE) AS hidden
"""

_VISIBILITY = """
WITH enabled_filters AS (
    -- DISTINCT is load-bearing: two enabled filters can share a prompt_hash
    -- (same prompt text under different names - adopting a preset and then
    -- pasting the same prompt does it). filter_pass dedupes per hash, so
    -- without this the passed_count could never reach COUNT(*) and the board
    -- would silently go empty.
    SELECT DISTINCT prompt_hash FROM user_filters WHERE user_id = %(uid)s AND enabled
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
        {criteria}
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


# Whitelisted server-side sort columns (all NULLS LAST so empty cells sink).
_SORTABLE = {
    "added_at": "j.created_at",
    "date_posted": "j.date_posted",
    "date_applied": "uj.date_applied",
    "company": "lower(j.company)",
    "title": "lower(j.title)",
    "source": "j.source",
    "status": "uj.status",
    "comp": "j.comp_max",
}

NOT_APPLIED = "not_applied"

# Canonical status vocabulary, backend-owned. The column stays free text (the
# sheet import brought arbitrary values), so options are served as this canon
# unioned with whatever statuses actually exist on the user's rows.
DEFAULT_STATUSES = [
    "Application Submitted",
    "Follow-up",
    "Recruiter Screen",
    "Online Assessment",
    "Interview",
    "Final Round",
    "Offer",
    "Accepted",
    "Rejected",
    "No Longer Interested",
]


@router.get("/user/jobs/options")
def job_options(user: AuthedUser = Depends(require_user)):
    """Everything the board's filter/edit controls need, generated from data
    instead of hardcoded in the client."""
    in_use = [
        r["status"]
        for r in db.query(
            "SELECT DISTINCT status FROM user_jobs "
            "WHERE user_id = %s AND status IS NOT NULL AND status != '' ORDER BY status",
            (user.id,),
        )
    ]
    statuses = DEFAULT_STATUSES + [s for s in in_use if s not in DEFAULT_STATUSES]
    sources = [
        r["source"]
        for r in db.query(
            "SELECT source FROM user_sources WHERE user_id = %s ORDER BY source",
            (user.id,),
        )
    ]
    return {"statuses": statuses, "not_applied_sentinel": NOT_APPLIED, "sources": sources}


@router.get("/user/jobs")
def list_jobs(
    limit: int = 200,
    offset: int = 0,
    cursor: Optional[int] = None,
    sort: str = "added_at",
    dir: str = "desc",
    search: Optional[str] = None,
    status: Optional[str] = None,
    statuses: Optional[str] = None,
    source: Optional[str] = None,
    include_hidden: bool = False,
    with_total: bool = False,
    user: AuthedUser = Depends(require_user),
):
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    settings = db.query_one(
        "SELECT bypass_sponsorship_filter, criteria FROM user_settings WHERE user_id = %s",
        (user.id,),
    )
    extra = []
    params: dict = {
        "uid": user.id,
        "limit": limit + 1,
        "offset": offset,
        "bypass_sponsorship": settings["bypass_sponsorship_filter"] if settings else True,
        **criteria.params(settings),
    }
    if not include_hidden:
        extra.append("AND COALESCE(uj.hidden, FALSE) = FALSE")
    if search:
        extra.append(
            "AND (j.company ILIKE %(search)s OR j.title ILIKE %(search)s OR j.url ILIKE %(search)s)"
        )
        params["search"] = f"%{search}%"
    wanted = [s.strip() for s in (statuses or "").split(",") if s.strip()]
    if status and status not in wanted:
        wanted.append(status)
    if wanted:
        named = [s for s in wanted if s != NOT_APPLIED]
        clauses = []
        if named:
            clauses.append("uj.status = ANY(%(statuses)s)")
            params["statuses"] = named
        if NOT_APPLIED in wanted:
            clauses.append("(uj.status IS NULL OR uj.status = '')")
        extra.append(f"AND ({' OR '.join(clauses)})")
    if source:
        extra.append("AND j.source = %(source)s")
        params["source"] = source

    filter_sql = "\n".join(extra)
    total = None
    if with_total:
        count_sql = _VISIBILITY.format(
            columns="COUNT(*) AS c", extra=filter_sql, criteria=criteria.SQL
        )
        row = db.query_one(count_sql, params)
        total = row["c"] if row else 0

    if cursor is not None:
        # Legacy cursor mode: fixed newest-first by id.
        order = "AND j.id < %(cursor)s\nORDER BY j.id DESC LIMIT %(limit)s"
        params["cursor"] = cursor
    else:
        sort_col = _SORTABLE.get(sort, "j.created_at")
        direction = "ASC" if dir == "asc" else "DESC"
        order = (
            f"ORDER BY {sort_col} {direction} NULLS LAST, j.id DESC "
            "LIMIT %(limit)s OFFSET %(offset)s"
        )
    sql = _VISIBILITY.format(
        columns=_JOB_ROW, extra=f"{filter_sql}\n{order}", criteria=criteria.SQL
    )
    rows = db.query(sql, params)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "rows": rows,
        "next_cursor": rows[-1]["job_id"] if cursor is not None and has_more and rows else None,
        "has_more": has_more,
        "offset": offset,
        "total": total,
    }


@router.patch("/user/jobs/{job_id}")
def patch_job(job_id: int, body: UserJobPatch, user: AuthedUser = Depends(require_user)):
    if not db.query_one("SELECT id FROM jobs WHERE id = %s", (job_id,)):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    autofilled = {}
    existing = None
    if "status" in fields or "date_applied" not in fields:
        existing = db.query_one(
            "SELECT status, date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user.id, job_id),
        )
    # Setting any real status implies the user acted on the job; stamp
    # date_applied once so they never have to fill it by hand.
    if fields.get("status") and "date_applied" not in fields:
        if not existing or existing["date_applied"] is None:
            fields["date_applied"] = datetime.date.today()
            autofilled["date_applied"] = fields["date_applied"].isoformat()
    if "status" in fields:
        old_status = existing["status"] if existing else None
        if (old_status or "") != (fields["status"] or ""):
            db.execute(
                "INSERT INTO user_job_history (user_id, job_id, old_status, new_status) "
                "VALUES (%s, %s, %s, %s)",
                (user.id, job_id, old_status, fields["status"]),
            )
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
    return {"ok": True, "autofilled": autofilled}


@router.get("/user/jobs/{job_id}/detail")
def job_detail(job_id: int, user: AuthedUser = Depends(require_user)):
    """Everything behind one board row: cached posting content, the user's own
    row + status history, and why the AI let it through (per-filter verdicts
    plus the closed/clearance checks)."""
    job = db.query_one(
        "SELECT id, url, raw_url, company, title, locations, terms, source, active, "
        "date_posted, comp_min, comp_max, comp_text, created_at FROM jobs WHERE id = %s",
        (job_id,),
    )
    if not job:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    content_row = db.query_one(
        "SELECT input_content, created_at FROM ai_queries "
        "WHERE url = %s AND check_type = 'content' AND input_content IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (job["url"],),
    )
    checks = db.query(
        """
        SELECT DISTINCT ON (check_type) check_type, status, reason, model, created_at
        FROM ai_queries
        WHERE url = %(url)s AND check_type IN ('closed', 'clearance')
          AND status IN ('passed', 'rejected')
        ORDER BY check_type, id DESC
        """,
        {"url": job["url"]},
    )
    filter_verdicts = db.query(
        """
        SELECT f.name, f.enabled, v.status, v.reason, v.model, v.created_at
        FROM user_filters f
        LEFT JOIN LATERAL (
            SELECT status, reason, model, created_at FROM ai_queries q
            WHERE q.url = %(url)s AND q.check_type = 'custom'
              AND q.prompt_hash = f.prompt_hash
              AND q.status IN ('passed', 'rejected')
            ORDER BY q.id DESC LIMIT 1
        ) v ON TRUE
        WHERE f.user_id = %(uid)s ORDER BY f.id
        """,
        {"url": job["url"], "uid": user.id},
    )
    return {
        "job": job,
        "row": db.query_one(
            "SELECT status, date_applied, notes, size, recruiter, connection1, "
            "connection2, documents, hidden, created_at, updated_at "
            "FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user.id, job_id),
        ),
        "history": db.query(
            "SELECT old_status, new_status, created_at FROM user_job_history "
            "WHERE user_id = %s AND job_id = %s ORDER BY id",
            (user.id, job_id),
        ),
        "content": (content_row or {}).get("input_content"),
        "content_fetched_at": (content_row or {}).get("created_at"),
        "checks": checks,
        "filter_verdicts": filter_verdicts,
    }


class ExplainBody(BaseModel):
    check: str


@router.post("/user/jobs/{job_id}/explain")
async def explain_check(
    job_id: int, body: ExplainBody, user: AuthedUser = Depends(require_user)
):
    """On-demand debugging: re-runs one check with the reason-ful schema and
    fuller reasoning (default verdicts skip reasons to save output tokens).
    Records a fresh verdict row (context 'explain') and returns the reason."""
    import dataclasses

    from api import budget
    from api import verdicts as _verdicts
    from api.worker import FilterVerdict
    from core.filters import build_custom_instructions
    from core.pittcsc_simplify import (
        CLEARANCE_INSTRUCTIONS,
        CLOSED_INSTRUCTIONS,
        ClearanceRequirementResponse,
        JobClosedResponse,
    )

    job = db.query_one("SELECT id, url, company, title FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    fresh, closure_signal = await _verdicts.refresh_content(
        job["url"], company=job["company"], job_title=job["title"], context="explain"
    )
    if fresh is None:
        gone = db.query_one(
            "SELECT status, reason FROM ai_queries WHERE url = %s AND check_type = 'closed' "
            "ORDER BY id DESC LIMIT 1",
            (job["url"],),
        )
        if closure_signal:
            return {
                "check": body.check, "status": "rejected",
                "reason": (gone or {}).get("reason", ""),
                "refetched": True, "closure_signal": closure_signal,
            }
        raise HTTPException(
            409,
            detail={"code": "NO_CONTENT", "message": "could not fetch this posting just now"},
        )
    content_row = {"input_content": fresh}
    ent = budget.get_entitlement(user)
    cfg = budget.resolve_ai_config(user.id, ent)
    cfg = dataclasses.replace(cfg, params={**cfg.params, "reasoning_effort": "medium"})

    check = body.check
    filter_name = prompt_hash = None
    if check == "closed":
        instructions, model_cls = CLOSED_INSTRUCTIONS, JobClosedResponse
        verdict_of = lambda p: (p.is_closed, p.reason or "")
    elif check == "clearance":
        instructions, model_cls = CLEARANCE_INSTRUCTIONS, ClearanceRequirementResponse
        verdict_of = lambda p: (
            p.requires_clearance_or_restrictions,
            p.reason or (p.restriction_type or ""),
        )
    elif check.startswith("filter:"):
        flt = db.query_one(
            "SELECT name, prompt, on_ambiguous, prompt_hash FROM user_filters "
            "WHERE user_id = %s AND id = %s",
            (user.id, int(check.split(":", 1)[1])),
        )
        if not flt:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
        instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
        model_cls = FilterVerdict
        verdict_of = lambda p: (p.should_filter, p.reason)
        filter_name = f"user{user.id}:{flt['name']}"
        prompt_hash = flt["prompt_hash"]
        check = "custom"
    else:
        raise HTTPException(
            400,
            detail={"code": "INVALID_CHECK", "message": "check must be closed, clearance, or filter:<id>"},
        )

    parsed, usage = await _verdicts.run_check(
        cfg, url=job["url"], check_type=check, instructions=instructions,
        input_text=content_row["input_content"][:60000], response_model=model_cls,
        verdict_of=verdict_of, company=job["company"], job_title=job["title"],
        filter_name=filter_name, prompt_hash=prompt_hash, context="explain",
    )
    budget.record_usage(
        user.id, cfg.key_source, "explain", cfg.model,
        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
        usage.get("total_tokens", 0),
    )
    rejected, reason = verdict_of(parsed)
    return {"check": body.check, "status": "rejected" if rejected else "passed", "reason": reason}


@router.delete("/user/jobs/{job_id}")
def delete_user_job(job_id: int, user: AuthedUser = Depends(require_user)):
    """Drops the user's board row only (the catalog job is untouched); a later
    run re-materializes it if it still passes their filters. Hide is the
    permanent alternative."""
    db.execute(
        "DELETE FROM user_jobs WHERE user_id = %s AND job_id = %s", (user.id, job_id)
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
            task = db.query_one(
                "INSERT INTO tasks (kind, payload) VALUES ('extract_upload', %s) RETURNING id",
                (db.jsonb({"job_id": row["id"], "user_id": user.id}),),
            )
            if task:
                events.publish_task(task["id"])
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
