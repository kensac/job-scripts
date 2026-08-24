from __future__ import annotations

import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db, events
from api.auth import AuthedUser, require_user

router = APIRouter(prefix="/admin")

ADMIN_GROUPS = {
    g.strip()
    for g in os.environ.get("JOBTRACKER_ADMIN_GROUPS", "infra-admins").split(",")
    if g.strip()
}

_SORTABLE = {
    "id",
    "created_at",
    "check_type",
    "status",
    "company",
    "total_tokens",
    "duration_ms",
}

_LIST_COLS = (
    "id, created_at, config_name, url, check_type, status, reason, model, "
    "company, job_title, prompt_tokens, completion_tokens, total_tokens, "
    "cached_tokens, reasoning_tokens, duration_ms, error"
)


def require_admin(user: AuthedUser = Depends(require_user)) -> AuthedUser:
    if not ADMIN_GROUPS.intersection(user.groups):
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "admin group required"})
    return user


def _where(
    check_type: Optional[str],
    status: Optional[str],
    config: Optional[str],
    url: Optional[str],
    q: Optional[str],
) -> tuple[str, dict]:
    clauses = []
    params: dict = {}
    if check_type:
        clauses.append("check_type = %(check_type)s")
        params["check_type"] = check_type
    if status:
        clauses.append("status = %(status)s")
        params["status"] = status
    if config:
        clauses.append("config_name = %(config)s")
        params["config"] = config
    if url:
        clauses.append("url = %(url)s")
        params["url"] = url
    if q:
        clauses.append(
            "(reason LIKE %(q)s OR url LIKE %(q)s OR company LIKE %(q)s "
            "OR job_title LIKE %(q)s OR input_content LIKE %(q)s)"
        )
        params["q"] = f"%{q}%"
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


class PresetBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=8000)
    on_ambiguous: Optional[str] = None
    fail_closed: Optional[bool] = None
    active: Optional[bool] = None


@router.get("/filter-presets")
def admin_list_presets(user: AuthedUser = Depends(require_admin)):
    return {"presets": db.query("SELECT * FROM filter_presets ORDER BY name")}


@router.post("/filter-presets")
def create_preset(body: PresetBody, user: AuthedUser = Depends(require_admin)):
    if not body.name or not body.prompt:
        raise HTTPException(
            400, detail={"code": "MISSING_FIELDS", "message": "name and prompt are required"}
        )
    if db.query_one("SELECT id FROM filter_presets WHERE name = %s", (body.name,)):
        raise HTTPException(409, detail={"code": "DUPLICATE_NAME", "message": "preset name exists"})
    return db.query_one(
        """
        INSERT INTO filter_presets (name, description, prompt, on_ambiguous, fail_closed, active)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            body.name,
            body.description or "",
            body.prompt,
            body.on_ambiguous or "keep",
            bool(body.fail_closed),
            body.active if body.active is not None else True,
        ),
    )


@router.patch("/filter-presets/{preset_id}")
def patch_preset(preset_id: int, body: PresetBody, user: AuthedUser = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    row = db.query_one(
        f"UPDATE filter_presets SET {cols}, updated_at = now() WHERE id = %(pid)s RETURNING *",
        {"pid": preset_id, **fields},
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown preset"})
    return row


@router.delete("/filter-presets/{preset_id}")
def delete_preset(preset_id: int, user: AuthedUser = Depends(require_admin)):
    db.execute("DELETE FROM filter_presets WHERE id = %s", (preset_id,))
    return {"ok": True}


@router.get("/source-requests")
def list_source_requests(status: str = "open", user: AuthedUser = Depends(require_admin)):
    where = "" if status == "all" else "WHERE sr.status = %(status)s"
    return {
        "rows": db.query(
            f"""
            SELECT sr.*, u.email AS requester_email, u.name AS requester_name
            FROM source_requests sr JOIN users u ON u.id = sr.user_id
            {where} ORDER BY sr.id DESC LIMIT 200
            """,
            {"status": status},
        )
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


@router.get("/sources")
def admin_list_sources(user: AuthedUser = Depends(require_admin)):
    return {
        "sources": db.query(
            """
            SELECT s.name, s.listings_url, s.description, s.active, s.created_at,
                   (SELECT COUNT(*) FROM jobs j WHERE j.source = s.name) AS jobs,
                   (SELECT COUNT(*) FROM user_sources us WHERE us.source = s.name) AS subscribers,
                   li.status AS last_ingest_status,
                   li.finished_at AS last_ingest_at,
                   li.error AS last_ingest_error
            FROM sources s
            LEFT JOIN LATERAL (
                SELECT status, finished_at, error FROM tasks
                WHERE kind = 'ingest_source' AND payload->>'source' = s.name
                ORDER BY id DESC LIMIT 1
            ) li ON TRUE
            ORDER BY s.active DESC, s.name
            """
        )
    }


class SourceBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    listings_url: Optional[str] = Field(default=None, max_length=1000)
    description: Optional[str] = Field(default=None, max_length=500)
    active: Optional[bool] = None


@router.post("/sources")
def create_source(body: SourceBody, user: AuthedUser = Depends(require_admin)):
    if not body.name or not body.listings_url:
        raise HTTPException(
            400, detail={"code": "MISSING_FIELDS", "message": "name and listings_url are required"}
        )
    if db.query_one("SELECT name FROM sources WHERE name = %s", (body.name,)):
        raise HTTPException(409, detail={"code": "DUPLICATE_NAME", "message": "source name exists"})
    return db.query_one(
        "INSERT INTO sources (name, listings_url, description, active) "
        "VALUES (%s, %s, %s, %s) RETURNING *",
        (
            body.name,
            body.listings_url,
            body.description or "",
            body.active if body.active is not None else True,
        ),
    )


@router.get("/users")
def list_users(user: AuthedUser = Depends(require_admin)):
    return {
        "users": db.query(
            """
            SELECT u.id, u.sub, u.email, u.name, u.groups, u.created_at, u.last_seen_at,
                   s.api_key_enc IS NOT NULL AS has_byo_key,
                   s.ai_provider, s.ai_model, s.bypass_sponsorship_filter,
                   (SELECT COUNT(*) FROM user_jobs uj WHERE uj.user_id = u.id) AS board_rows,
                   (SELECT COUNT(*) FROM user_filters uf
                    WHERE uf.user_id = u.id AND uf.enabled) AS enabled_filters,
                   (SELECT COUNT(*) FROM user_sources us WHERE us.user_id = u.id) AS sources,
                   COALESCE((SELECT SUM(a.total_tokens) FROM api_usage a
                             WHERE a.user_id = u.id AND a.key_source = 'owner'
                               AND a.created_at > now() - interval '7 days'), 0) AS owner_tokens_week
            FROM users u LEFT JOIN user_settings s ON s.user_id = u.id
            ORDER BY u.last_seen_at DESC
            """
        )
    }


@router.get("/tasks")
def list_tasks(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 100,
    user: AuthedUser = Depends(require_admin),
):
    limit = max(1, min(limit, 500))
    clauses, params = [], {"limit": limit}
    if status:
        clauses.append("status = %(status)s")
        params["status"] = status
    if kind:
        clauses.append("kind = %(kind)s")
        params["kind"] = kind
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query(
        f"""
        SELECT id, kind, payload, status, attempts, progress, error,
               created_at, started_at, last_heartbeat, finished_at
        FROM tasks {where} ORDER BY id DESC LIMIT %(limit)s
        """,
        params,
    )
    summary = db.query(
        "SELECT kind, status, COUNT(*) AS count FROM tasks GROUP BY kind, status ORDER BY kind, status"
    )
    return {"rows": rows, "summary": summary}


class IngestBody(BaseModel):
    sources: Optional[List[str]] = None


@router.post("/ingest")
def trigger_ingest(body: IngestBody, user: AuthedUser = Depends(require_admin)):
    """Off-cycle pull: enqueue ingest tasks now (no dedupe, runs regardless of
    the hourly cycle). Omit sources to pull everything active."""
    active = {r["name"] for r in db.query("SELECT name FROM sources WHERE active")}
    wanted = body.sources if body.sources else sorted(active)
    unknown = [s for s in wanted if s not in active]
    if unknown:
        raise HTTPException(
            400, detail={"code": "UNKNOWN_SOURCE", "message": f"unknown or inactive: {unknown}"}
        )
    import time as _time

    cycle = f"manual-{user.id}-{int(_time.time())}"
    task_ids = []
    for name in wanted:
        row = db.query_one(
            "INSERT INTO tasks (kind, payload) VALUES ('ingest_source', %s) RETURNING id",
            (db.jsonb({"source": name, "cycle": cycle}),),
        )
        assert row is not None
        events.publish_task(row["id"])
        task_ids.append({"source": name, "task_id": row["id"]})
    return {"tasks": task_ids}


class SourceGroupBody(BaseModel):
    members: Optional[List[str]] = None
    description: Optional[str] = Field(default=None, max_length=500)
    active: Optional[bool] = None


@router.post("/source-groups/{name}")
def upsert_source_group(name: str, body: SourceGroupBody, user: AuthedUser = Depends(require_admin)):
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
        {"name": name, "members": body.members, "description": body.description, "active": body.active},
    )
    return row


@router.patch("/sources/{name}")
def patch_source(name: str, body: SourceBody, user: AuthedUser = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True, exclude={"name"})
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    row = db.query_one(
        f"UPDATE sources SET {cols} WHERE name = %(name)s RETURNING *",
        {"name": name, **fields},
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    return row


_CONFIG_KEYS = {"signups_enabled"}


@router.get("/config")
def get_config(user: AuthedUser = Depends(require_admin)):
    rows = db.query("SELECT key, value FROM app_config ORDER BY key")
    return {"config": {r["key"]: r["value"] for r in rows}}


class ConfigPut(BaseModel):
    value: bool


@router.put("/config/{key}")
def put_config(key: str, body: ConfigPut, user: AuthedUser = Depends(require_admin)):
    if key not in _CONFIG_KEYS:
        raise HTTPException(
            400, detail={"code": "UNKNOWN_KEY", "message": f"key must be one of {sorted(_CONFIG_KEYS)}"}
        )
    db.execute(
        "INSERT INTO app_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, db.jsonb(body.value)),
    )
    return {"key": key, "value": body.value}


@router.get("/reports")
def list_reports(
    status: str = "open",
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
):
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    where = "" if status == "all" else "WHERE r.status = %(status)s"
    total_row = db.query_one(
        f"SELECT COUNT(*) AS c FROM reports r {where}", {"status": status}
    )
    rows = db.query(
        f"""
        SELECT r.*, u.email AS reporter_email, u.name AS reporter_name,
               j.url, j.company, j.title, j.source, j.extraction_status
        FROM reports r
        JOIN users u ON u.id = r.user_id
        JOIN jobs j ON j.id = r.job_id
        {where}
        ORDER BY r.id DESC LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"status": status, "limit": page_size, "offset": (page - 1) * page_size},
    )
    return {
        "rows": rows,
        "total": total_row["c"] if total_row else 0,
        "page": page,
        "page_size": page_size,
    }


class ResolveReport(BaseModel):
    action: str
    note: str = ""


@router.post("/reports/{report_id}/resolve")
def resolve_report(
    report_id: int, body: ResolveReport, user: AuthedUser = Depends(require_admin)
):
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
    company: Optional[str] = None
    title: Optional[str] = None
    locations: Optional[List[str]] = None
    terms: Optional[List[str]] = None
    active: Optional[bool] = None


@router.patch("/jobs/{job_id}")
def patch_catalog_job(
    job_id: int, body: JobCorrection, user: AuthedUser = Depends(require_admin)
):
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


@router.post("/jobs/{job_id}/reparse")
def reparse_job(job_id: int, user: AuthedUser = Depends(require_admin)):
    job = db.query_one("SELECT id FROM jobs WHERE id = %s", (job_id,))
    if not job:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    db.execute("UPDATE jobs SET extraction_status = 'pending' WHERE id = %s", (job_id,))
    row = db.query_one(
        "INSERT INTO tasks (kind, payload) VALUES ('extract_upload', %s) RETURNING id",
        (db.jsonb({"job_id": job_id, "user_id": user.id, "force": True}),),
    )
    assert row is not None
    events.publish_task(row["id"])
    return {"task_id": row["id"]}


class GroupBudgetPut(BaseModel):
    weekly_token_budget: Optional[int] = Field(default=None, ge=0)


@router.get("/group-budgets")
def list_group_budgets(user: AuthedUser = Depends(require_admin)):
    return {
        "groups": db.query(
            "SELECT group_name, weekly_token_budget FROM group_budgets ORDER BY group_name"
        )
    }


@router.put("/group-budgets/{group_name}")
def put_group_budget(
    group_name: str, body: GroupBudgetPut, user: AuthedUser = Depends(require_admin)
):
    db.execute(
        """
        INSERT INTO group_budgets (group_name, weekly_token_budget) VALUES (%s, %s)
        ON CONFLICT (group_name) DO UPDATE SET weekly_token_budget = EXCLUDED.weekly_token_budget
        """,
        (group_name, body.weekly_token_budget),
    )
    return {"group_name": group_name, "weekly_token_budget": body.weekly_token_budget}


@router.delete("/group-budgets/{group_name}")
def delete_group_budget(group_name: str, user: AuthedUser = Depends(require_admin)):
    db.execute("DELETE FROM group_budgets WHERE group_name = %s", (group_name,))
    return {"ok": True}


@router.get("/queries")
def list_queries(
    check_type: Optional[str] = None,
    status: Optional[str] = None,
    config: Optional[str] = None,
    url: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "id",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
):
    where, params = _where(check_type, status, config, url, q)
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    total_row = db.query_one(f"SELECT COUNT(*) AS c FROM ai_queries {where}", params)
    sort_col = sort if sort in _SORTABLE else "id"
    direction = "ASC" if dir == "asc" else "DESC"
    rows = db.query(
        f"SELECT {_LIST_COLS} FROM ai_queries {where} "
        f"ORDER BY {sort_col} {direction} LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    )
    return {
        "rows": rows,
        "total": total_row["c"] if total_row else 0,
        "page": page,
        "page_size": page_size,
    }


@router.get("/queries/{query_id}")
def get_query(query_id: int, user: AuthedUser = Depends(require_admin)):
    row = db.query_one("SELECT * FROM ai_queries WHERE id = %s", (query_id,))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown query"})
    return row


class DeleteQueries(BaseModel):
    ids: List[int] = Field(min_length=1, max_length=10000)


@router.post("/queries/delete")
def delete_queries(body: DeleteQueries, user: AuthedUser = Depends(require_admin)):
    with db.pool.connection() as conn:
        result = conn.execute("DELETE FROM ai_queries WHERE id = ANY(%s)", (body.ids,))
        deleted = result.rowcount
    return {"deleted": deleted}


@router.get("/jobs")
def list_jobs(
    q: Optional[str] = None,
    config: Optional[str] = None,
    verdict: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
):
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    sub = ["url IS NOT NULL"]
    params: dict = {}
    if q:
        sub.append(
            "(url LIKE %(q)s OR company LIKE %(q)s OR job_title LIKE %(q)s OR reason LIKE %(q)s)"
        )
        params["q"] = f"%{q}%"
    if config:
        sub.append("config_name = %(config)s")
        params["config"] = config
    having = ""
    if verdict == "rejected":
        having = "HAVING SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) > 0"
    elif verdict == "passed":
        having = (
            "HAVING SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) = 0 "
            "AND SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) > 0"
        )
    base = f"""
        SELECT url,
            MAX(company) AS company,
            MAX(job_title) AS job_title,
            MAX(config_name) AS config_name,
            COUNT(*) AS checks,
            SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            MAX(created_at) AS last_seen
        FROM ai_queries
        WHERE url IN (SELECT url FROM ai_queries WHERE {' AND '.join(sub)})
        GROUP BY url
        {having}
    """
    total_row = db.query_one(f"SELECT COUNT(*) AS c FROM ({base}) sub", params)
    rows = db.query(
        f"{base} ORDER BY last_seen DESC LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    )
    for r in rows:
        r["verdict"] = (
            "rejected" if r["rejected"] > 0 else "passed" if r["passed"] > 0 else "other"
        )
    return {
        "rows": rows,
        "total": total_row["c"] if total_row else 0,
        "page": page,
        "page_size": page_size,
    }


@router.get("/jobs/responses")
def job_responses(url: str, user: AuthedUser = Depends(require_admin)):
    return {
        "rows": db.query(
            "SELECT * FROM ai_queries WHERE url = %s ORDER BY id ASC", (url,)
        )
    }


@router.get("/jobs/timeline")
def job_timeline(url: str, user: AuthedUser = Depends(require_admin)):
    return {
        "rows": db.query(
            "SELECT id, created_at, config_name, check_type, status, reason, model, "
            "total_tokens, duration_ms, error "
            "FROM ai_queries WHERE url = %s ORDER BY id ASC",
            (url,),
        )
    }


@router.get("/options")
def options(user: AuthedUser = Depends(require_admin)):
    def distinct(col: str) -> List[str]:
        return [
            r["v"]
            for r in db.query(
                f"SELECT DISTINCT {col} AS v FROM ai_queries "
                f"WHERE {col} IS NOT NULL ORDER BY {col}"
            )
        ]

    return {
        "check_types": distinct("check_type"),
        "statuses": distinct("status"),
        "configs": distinct("config_name"),
    }


@router.get("/stats")
def stats(user: AuthedUser = Depends(require_admin)):
    totals = db.query_one(
        """
        SELECT COUNT(*) AS queries,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
        FROM ai_queries
        """
    )
    by_check_type = db.query(
        """
        SELECT check_type, COUNT(*) AS count,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens
        FROM ai_queries GROUP BY check_type ORDER BY count DESC
        """
    )
    by_status = db.query(
        "SELECT status, COUNT(*) AS count FROM ai_queries GROUP BY status ORDER BY count DESC"
    )
    by_day = db.query(
        """
        SELECT substr(created_at, 1, 10) AS day,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
        FROM ai_queries GROUP BY day ORDER BY day ASC
        """
    )
    return {
        "totals": totals,
        "by_check_type": by_check_type,
        "by_status": by_status,
        "by_day": by_day,
    }
