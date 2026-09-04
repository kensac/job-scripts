from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import ai, db, events, health
from api.auth import AuthedUser, require_user
from core import pricing, reason_taxonomy

logger = logging.getLogger("jobtracker_api")

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
    "cached_tokens, reasoning_tokens, duration_ms, error, worker, "
    "filter_name, prompt_hash, "
    # Correlated lookups rather than a join: ai_queries and jobs share several
    # column names, so joining would make every existing filter ambiguous.
    "(SELECT j.id FROM jobs j WHERE j.url = ai_queries.url) AS job_id, "
    "(SELECT j.source FROM jobs j WHERE j.url = ai_queries.url) AS source"
)


def require_admin(user: AuthedUser = Depends(require_user)) -> AuthedUser:
    if not ADMIN_GROUPS.intersection(user.groups):
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "admin group required"})
    return user


def _where(
    check_type: str | None,
    status: str | None,
    config: str | None,
    url: str | None,
    q: str | None,
    deep: bool = False,
    sources: str | None = None,
    reason_group: str | None = None,
    evidence_missing: bool = False,
    prompt_hash: str | None = None,
) -> tuple[str, dict]:
    clauses = []
    params: dict = {}
    wanted_sources = [s.strip() for s in (sources or "").split(",") if s.strip()]
    if wanted_sources:
        # ai_queries is keyed by url; source lives on the job. Subquery instead
        # of a join keeps this composable with the existing count/list queries.
        clauses.append("url IN (SELECT url FROM jobs WHERE source = ANY(%(sources)s))")
        params["sources"] = wanted_sources
    if check_type:
        clauses.append("check_type = %(check_type)s")
        params["check_type"] = check_type
    if status:
        clauses.append("status = %(status)s")
        params["status"] = status
    if config:
        clauses.append("config_name = %(config)s")
        params["config"] = config
    # A filter-insights count is scoped to ONE prompt version, because a
    # prompt_hash IS the filter as it was actually run - the same name has been
    # two different prompts. A drill-through that carried only the reason group
    # would open that group across every version, so the count and the rows it
    # opened could not match, and not by a little.
    if prompt_hash:
        clauses.append("prompt_hash = %(prompt_hash)s")
        params["prompt_hash"] = prompt_hash
    if url:
        clauses.append("url = %(url)s")
        params["url"] = url
    if q:
        # Default search hits only trigram-indexed columns; including
        # input_content (unindexed page dumps) forces a sequential scan, so
        # it's opt-in via deep=true.
        cols = (
            "(reason ILIKE %(q)s OR url ILIKE %(q)s OR company ILIKE %(q)s OR job_title ILIKE %(q)s"
        )
        if deep:
            cols += " OR input_content ILIKE %(q)s"
        clauses.append(cols + ")")
        params["q"] = f"%{q}%"
    # The filter-insights aggregate classifies reasons in Python; this
    # drill-through has to reproduce that selection in SQL. If the two ever
    # disagreed, a count would link to a different set of rows than it counted,
    # which is worse than not linking at all - so both spellings are generated
    # from core.reason_taxonomy and pinned equal by a test.
    #
    # `~*` is never itself indexed: the trigram index on `reason` cannot serve
    # a general regex (GIN trigram covers LIKE and similarity only), so do not
    # read that index's existence as meaning this is.
    #
    # What it scans depends on what it is composed with. A drill-through link
    # carries check_type and prompt_hash, and idx_ai_queries_prompt_hash is on
    # (check_type, prompt_hash) - so those two select the rows first and the
    # regex only runs over that one prompt version. Used alone, it scans all
    # 78k, which is still only tens of milliseconds.
    if reason_group:
        clauses.append("reason ~* %(reason_group)s")
        params["reason_group"] = reason_taxonomy.sql_pattern(reason_group)
    if evidence_missing:
        clauses.append("reason ~* %(evidence_missing)s")
        params["evidence_missing"] = reason_taxonomy.EVIDENCE_MISSING_SQL
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


class PresetBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    on_ambiguous: str | None = None
    fail_closed: bool | None = None
    active: bool | None = None


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
def list_source_requests(
    status: str = "open",
    limit: int = 50,
    offset: int = 0,
    user: AuthedUser = Depends(require_admin),
):
    limit = max(1, min(limit, 200))
    where = "" if status == "all" else "WHERE sr.status = %(status)s"
    rows = db.query(
        f"""
        SELECT sr.*, u.email AS requester_email, u.name AS requester_name
        FROM source_requests sr JOIN users u ON u.id = sr.user_id
        {where} ORDER BY sr.id DESC LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"status": status, "limit": limit + 1, "offset": max(0, offset)},
    )
    # The badge on the Requests tab used to show the page size and read as
    # the count; at 389 sources a queue of requests can exceed a page.
    total = db.query_one(
        f"SELECT count(*) AS n FROM source_requests sr {where}", {"status": status}
    )
    return {
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


@router.get("/sources")
def admin_list_sources(user: AuthedUser = Depends(require_admin)):
    from core import boards

    rows = db.query(
        """
            SELECT s.name, s.listings_url, s.description, s.active, s.created_at,
                   s.company, s.title_pattern, s.ingest_interval_hours,
                   -- Bundle membership per row, so grouping the list needs no
                   -- client-side join of 389 rows against every bundle.
                   COALESCE((SELECT array_agg(g.name ORDER BY g.name) FROM source_groups g
                             WHERE s.name = ANY(g.members)), '{}') AS groups,
                   (SELECT COUNT(*) FROM jobs j WHERE j.source = s.name) AS jobs,
                   (SELECT COUNT(*) FROM user_sources us WHERE us.source = s.name) AS subscribers,
                   li.status AS last_ingest_status,
                   li.finished_at AS last_ingest_at,
                   li.error AS last_ingest_error,
                   -- The number that retires a source, and the only one that
                   -- can. last_ingest_at says the fetch worked; this says the
                   -- fetch found anything we had not already seen. They
                   -- diverge, and the gap IS the signal: fulltime_ouckah has
                   -- 215 successful ingests and has produced no new posting
                   -- since the catalog was reseeded, reporting green every
                   -- hour. Six configured sources have never produced one at
                   -- all. Nothing in this response could previously say so.
                   (SELECT MAX(j.created_at) FROM jobs j WHERE j.source = s.name)
                       AS last_new_posting_at
            FROM sources s
            LEFT JOIN LATERAL (
                SELECT status, finished_at, error FROM tasks
                WHERE kind = 'ingest_source' AND payload->>'source' = s.name
                ORDER BY id DESC LIMIT 1
            ) li ON TRUE
            ORDER BY s.active DESC, s.name
            """
    )
    # The format is read off the URL, never stored, so it cannot drift from
    # what ingest will actually do with the row. This is the top-level
    # category the switch endpoint selects by.
    for r in rows:
        r["kind"] = boards.kind(r["listings_url"])
    return {"sources": rows}


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
    from core import boards

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
def create_source(body: SourceBody, user: AuthedUser = Depends(require_admin)):
    if not body.name or not body.listings_url:
        raise HTTPException(
            400, detail={"code": "MISSING_FIELDS", "message": "name and listings_url are required"}
        )
    _check_source(body.listings_url, body.company, body.title_pattern)
    if db.query_one("SELECT name FROM sources WHERE name = %s", (body.name,)):
        raise HTTPException(409, detail={"code": "DUPLICATE_NAME", "message": "source name exists"})
    return db.query_one(
        "INSERT INTO sources (name, listings_url, description, active, company, title_pattern, "
        "ingest_interval_hours) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
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


@router.get("/users")
def list_users(
    limit: int = 50,
    offset: int = 0,
    user: AuthedUser = Depends(require_admin),
):
    limit = max(1, min(limit, 200))
    rows = db.query(
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
        ORDER BY u.last_seen_at DESC LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"limit": limit + 1, "offset": max(0, offset)},
    )
    return {"users": rows[:limit], "has_more": len(rows) > limit}


@router.get("/users/{user_id}")
def user_detail(user_id: int, user: AuthedUser = Depends(require_admin)):
    u = db.query_one(
        """
        SELECT u.id, u.sub, u.email, u.name, u.groups, u.created_at, u.last_seen_at,
               s.ai_provider, s.ai_model, s.ai_params, s.bypass_sponsorship_filter,
               s.criteria, s.api_key_enc IS NOT NULL AS has_byo_key
        FROM users u LEFT JOIN user_settings s ON s.user_id = u.id
        WHERE u.id = %s
        """,
        (user_id,),
    )
    if not u:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown user"})
    from api import budget as _budget

    # The Users page showed a weekly token total one click from a page showing
    # a 5,000,000 budget, with nothing saying which cap applied to whom - which
    # invited the reading that the owner was over budget when his group is
    # uncapped. Resolve it here rather than leaving the UI to infer.
    groups = u.get("groups") or []
    owner_key, weekly_cap = _budget._owner_budget(groups)
    granting = db.query(
        "SELECT group_name, weekly_token_budget FROM group_budgets "
        "WHERE group_name = ANY(%s) ORDER BY group_name",
        (groups,),
    )
    return {
        "user": u,
        "budget": {
            "owner_key": owner_key,
            # None means uncapped, which is a real answer and not a missing one.
            "weekly_token_budget": weekly_cap,
            "spent_this_week": _budget.spent_this_week(user_id) if owner_key else 0,
            "granted_by": granting,
        },
        "spend_by_day": db.query(
            """
            SELECT created_at::date AS day, key_source,
                   SUM(total_tokens) AS tokens, COUNT(*) AS calls
            FROM api_usage WHERE user_id = %s AND created_at > now() - interval '30 days'
            GROUP BY 1, 2 ORDER BY 1
            """,
            (user_id,),
        ),
        "spend_by_purpose": db.query(
            """
            SELECT purpose, model, SUM(total_tokens) AS tokens, COUNT(*) AS calls
            FROM api_usage WHERE user_id = %s GROUP BY 1, 2 ORDER BY 3 DESC
            """,
            (user_id,),
        ),
        "board": db.query(
            """
            SELECT COALESCE(NULLIF(status, ''), 'not_applied') AS status,
                   COUNT(*) AS count, COUNT(*) FILTER (WHERE hidden) AS hidden
            FROM user_jobs WHERE user_id = %s GROUP BY 1 ORDER BY 2 DESC
            """,
            (user_id,),
        ),
        "filters": db.query(
            "SELECT id, name, enabled, on_ambiguous, fail_closed, updated_at "
            "FROM user_filters WHERE user_id = %s ORDER BY id",
            (user_id,),
        ),
        "sources": [
            r["source"]
            for r in db.query(
                "SELECT source FROM user_sources WHERE user_id = %s ORDER BY source",
                (user_id,),
            )
        ],
        "uploads": db.query_one(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE extraction_status = 'failed') AS failed "
            "FROM jobs WHERE uploaded_by = %s",
            (user_id,),
        ),
        "reports": db.query_one(
            "SELECT COUNT(*) FILTER (WHERE status = 'open') AS open, COUNT(*) AS total "
            "FROM reports WHERE user_id = %s",
            (user_id,),
        ),
        "recent_tasks": db.query(
            """
            SELECT id, kind, status, worker, created_at, finished_at
            FROM tasks WHERE payload->>'user_id' = %s ORDER BY id DESC LIMIT 10
            """,
            (str(user_id),),
        ),
    }


@router.get("/tasks")
def list_tasks(
    status: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    limit: int = 100,
    before_id: int | None = None,
    user: AuthedUser = Depends(require_admin),
):
    # id-cursor pagination: stable under new tasks arriving while the admin
    # loads more (offset would shift and duplicate rows).
    limit = max(1, min(limit, 500))
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit + 1}
    if status:
        clauses.append("status = %(status)s")
        params["status"] = status
    if kind:
        clauses.append("kind = %(kind)s")
        params["kind"] = kind
    if source:
        # Ingest tasks are most of the queue at 389 boards; this cuts it to
        # one board's history.
        clauses.append("payload->>'source' = %(source)s")
        params["source"] = source
    if before_id is not None:
        clauses.append("id < %(before_id)s")
        params["before_id"] = before_id
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query(
        f"""
        SELECT id, kind, payload, status, attempts, worker, progress, error,
               created_at, started_at, last_heartbeat, finished_at
        FROM tasks {where} ORDER BY id DESC LIMIT %(limit)s
        """,
        params,
    )
    summary = db.query(
        "SELECT kind, status, COUNT(*) AS count FROM tasks GROUP BY kind, status ORDER BY kind, status"
    )
    return {"rows": rows[:limit], "has_more": len(rows) > limit, "summary": summary}


AUTHENTIK_URL = os.environ.get("AUTHENTIK_URL", "").rstrip("/")
AUTHENTIK_INVITE_TOKEN = os.environ.get("AUTHENTIK_INVITE_TOKEN", "")
AUTHENTIK_INVITE_FLOW = os.environ.get("AUTHENTIK_INVITE_FLOW", "jobtracker-enrollment")
# The invite service account can't read flows (403 by design), so the flow is
# addressed by its UUID, not resolved by slug.
AUTHENTIK_INVITE_FLOW_PK = os.environ.get(
    "AUTHENTIK_INVITE_FLOW_PK", "ecb38a8d-47a5-4eb5-afd1-fb2a480d144e"
)


def _invites_configured() -> bool:
    return bool(AUTHENTIK_URL and AUTHENTIK_INVITE_TOKEN and AUTHENTIK_INVITE_FLOW_PK)


def _authentik_client():
    import httpx

    return httpx.Client(
        base_url=f"{AUTHENTIK_URL}/api/v3",
        headers={"Authorization": f"Bearer {AUTHENTIK_INVITE_TOKEN}"},
        timeout=15,
    )


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/invites")
def create_invite(body: InviteBody, user: AuthedUser = Depends(require_admin)):
    """Email-only onboarding: creates a single-use Authentik invitation bound
    to the jobtracker enrollment flow and emails the link. The invitee picks
    their own username/name/password during enrollment."""
    if not _invites_configured():
        raise HTTPException(
            503,
            detail={"code": "INVITES_NOT_CONFIGURED", "message": "authentik invite env missing"},
        )
    import datetime as _dt
    import re as _re

    from api import mail

    email = body.email.strip().lower()
    expires = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=7)).isoformat()
    slug = _re.sub(r"[^a-z0-9]+", "-", email).strip("-")
    with _authentik_client() as ak:
        resp = ak.post(
            "/stages/invitation/invitations/",
            json={
                "name": f"jobtracker-{slug}-{int(_dt.datetime.now(_dt.UTC).timestamp())}",
                "expires": expires,
                "fixed_data": {"email": email},
                "single_use": True,
                "flow": AUTHENTIK_INVITE_FLOW_PK,
            },
        )
        if resp.status_code >= 300:
            raise HTTPException(
                502,
                detail={
                    "code": "AUTHENTIK_ERROR",
                    "message": f"invitation create failed ({resp.status_code})",
                },
            )
        inv = resp.json()
    invite_url = f"{AUTHENTIK_URL}/if/flow/{AUTHENTIK_INVITE_FLOW}/?itoken={inv['pk']}"
    emailed = False
    if mail.configured():
        try:
            mail.send_invite(email, invite_url)
            emailed = True
        except Exception:
            # The invite itself succeeded; only delivery failed. Report it
            # rather than leaving emailed=False unexplained.
            logger.exception(f"invite created but email to {email} failed")
    return {
        "ok": True,
        "invite_url": invite_url,
        "expires": expires,
        "emailed": emailed,
        "pk": inv["pk"],
    }


@router.get("/invites")
def list_invites(user: AuthedUser = Depends(require_admin)):
    if not _invites_configured():
        return {"rows": [], "configured": False}
    with _authentik_client() as ak:
        resp = ak.get(
            "/stages/invitation/invitations/", params={"flow__slug": AUTHENTIK_INVITE_FLOW}
        )
        if resp.status_code >= 300:
            raise HTTPException(
                502, detail={"code": "AUTHENTIK_ERROR", "message": "invitation list failed"}
            )
        data = resp.json()
    rows = [
        {
            "pk": r["pk"],
            "email": (r.get("fixed_data") or {}).get("email", ""),
            "expires": r.get("expires"),
            "single_use": r.get("single_use", True),
        }
        for r in data.get("results", [])
    ]
    return {"rows": rows, "configured": True}


@router.delete("/invites/{pk}")
def revoke_invite(pk: str, user: AuthedUser = Depends(require_admin)):
    if not _invites_configured():
        raise HTTPException(
            503,
            detail={"code": "INVITES_NOT_CONFIGURED", "message": "authentik invite env missing"},
        )
    with _authentik_client() as ak:
        resp = ak.delete(f"/stages/invitation/invitations/{pk}/")
        if resp.status_code >= 300 and resp.status_code != 404:
            raise HTTPException(
                502, detail={"code": "AUTHENTIK_ERROR", "message": "invitation revoke failed"}
            )
    return {"ok": True}


class CancelTasksBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


@router.post("/tasks/cancel")
def cancel_tasks(body: CancelTasksBody, user: AuthedUser = Depends(require_admin)):
    """Cancel queued or in-flight tasks. Running workers notice via their
    mid-task cancellation checks; pending chunks of a cancelled parent are
    swept by the worker's reconciler. A task parked on provider batches
    (awaiting_batch) is cancellable too - it holds no worker, so nothing
    notices otherwise and it would sit until its batches landed."""
    rows = db.query(
        """
        UPDATE tasks SET status = 'cancelled', error = 'cancelled by admin',
                         finished_at = now()
        WHERE id = ANY(%s)
          AND status IN ('pending', 'waiting', 'running', 'awaiting_batch')
        RETURNING id
        """,
        (body.ids,),
    )
    cancelled = [r["id"] for r in rows]
    for task_id in cancelled:
        events.publish_task(task_id)
    return {"cancelled": cancelled, "skipped": [i for i in body.ids if i not in cancelled]}


@router.get("/batches")
def list_batches(hours: int = 72, user: AuthedUser = Depends(require_admin)):
    """Provider batch jobs: what's pending at OpenAI right now, and recent
    history. Pending first, then newest."""
    hours = max(1, min(hours, 720))
    rows = db.query(
        """
        SELECT b.id, b.provider_batch_id, b.task_id, b.purpose, b.model,
               b.requests, b.completed, b.failed_count, b.status,
               b.est_tokens, b.input_tokens, b.output_tokens, b.est_cost_usd,
               b.submitted_at, b.updated_at, b.completed_at,
               t.kind AS task_kind, t.status AS task_status
        FROM ai_batches b LEFT JOIN tasks t ON t.id = b.task_id
        WHERE b.status NOT IN ('completed', 'failed', 'expired', 'cancelled')
           OR b.submitted_at > now() - make_interval(hours => %(hours)s)
        ORDER BY (b.status IN ('completed', 'failed', 'expired', 'cancelled')), b.id DESC
        LIMIT 200
        """,
        {"hours": hours},
    )
    return {"rows": rows}


@router.get("/workers")
def list_workers(user: AuthedUser = Depends(require_admin)):
    """Live fleet view: every worker's heartbeat, what it's running right now,
    and its last-24h throughput."""
    rows = db.query(
        """
        SELECT w.name, w.started_at, w.last_seen, w.current_task_id,
               now() - w.last_seen < interval '90 seconds' AS alive,
               t.kind AS task_kind, t.status AS task_status, t.progress AS task_progress,
               t.started_at AS task_started_at,
               stats.done_24h, stats.failed_24h
        FROM worker_status w
        LEFT JOIN tasks t ON t.id = w.current_task_id AND t.status = 'running'
        LEFT JOIN LATERAL (
            SELECT COUNT(*) FILTER (WHERE s.status = 'done') AS done_24h,
                   COUNT(*) FILTER (WHERE s.status = 'failed') AS failed_24h
            FROM tasks s WHERE s.worker = w.name
              AND s.finished_at > now() - interval '24 hours'
        ) stats ON TRUE
        ORDER BY w.name
        """
    )
    return {"rows": rows}


@router.get("/queries/options")
def query_options(user: AuthedUser = Depends(require_admin)):
    """Filter vocabularies generated from live data, so the admin dropdowns
    can never drift from what actually exists."""

    def col(sql: str, key: str) -> list[str]:
        return [r[key] for r in db.query(sql) if r[key]]

    return {
        "sources": col("SELECT name FROM sources WHERE active ORDER BY name", "name"),
        "check_types": col(
            "SELECT DISTINCT check_type FROM ai_queries WHERE check_type IS NOT NULL "
            "ORDER BY check_type",
            "check_type",
        ),
        "statuses": col(
            "SELECT DISTINCT status FROM ai_queries WHERE status IS NOT NULL ORDER BY status",
            "status",
        ),
        # Pipeline context (which code path decided), not the dead legacy
        # config names — only values seen in the last 30 days.
        "contexts": col(
            "SELECT DISTINCT config_name FROM ai_queries WHERE config_name IS NOT NULL "
            "AND created_at > now() - interval '30 days' ORDER BY config_name",
            "config_name",
        ),
        "workers": col(
            "SELECT DISTINCT worker FROM ai_queries WHERE worker IS NOT NULL "
            "AND created_at > now() - interval '30 days' ORDER BY worker",
            "worker",
        ),
    }


@router.get("/batches/{provider_batch_id}/jobs")
def batch_jobs(
    provider_batch_id: str,
    limit: int = 200,
    offset: int = 0,
    user: AuthedUser = Depends(require_admin),
):
    """Every verdict this batch produced, with its own token cost — so a batch
    is inspectable down to the individual job. Paged: a batch is up to 500
    requests and the drill-down rendered every one of them."""
    batch = db.query_one(
        "SELECT * FROM ai_batches WHERE provider_batch_id = %s", (provider_batch_id,)
    )
    if not batch:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown batch"})
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    total = db.query_one(
        "SELECT count(*) AS n FROM ai_queries WHERE batch_id = %s", (provider_batch_id,)
    )
    rows = db.query(
        """
        SELECT q.id, q.url, q.check_type, q.status, q.reason, q.company, q.job_title,
               q.prompt_tokens, q.completion_tokens, q.total_tokens, q.cached_tokens,
               q.created_at, j.source, j.id AS job_id
        FROM ai_queries q LEFT JOIN jobs j ON j.url = q.url
        WHERE q.batch_id = %s ORDER BY q.id LIMIT %s OFFSET %s
        """,
        (provider_batch_id, limit, offset),
    )
    for r in rows:
        cost = pricing.estimate_cost_usd(
            batch["model"],
            r["prompt_tokens"],
            r["completion_tokens"],
            cached_tokens=r.get("cached_tokens"),
            batched=True,
        )
        r["est_cost_usd"] = round(float(cost), 6) if cost is not None else None
    n = total["n"] if total else 0
    return {"batch": batch, "rows": rows, "total": n, "has_more": offset + len(rows) < n}


class RunCheckBody(BaseModel):
    job_id: int
    check: str
    with_reason: bool = True


@router.post("/checks/run")
async def run_single_check(body: RunCheckBody, user: AuthedUser = Depends(require_admin)):
    """Manually re-run one check on one job, ignoring the cached verdict. The
    fresh row becomes the latest for that (url, check_type), so visibility
    re-derives from it immediately — no downstream re-run needed, since
    visibility is a read-time predicate rather than stored derived state."""
    from api import verdicts as _verdicts
    from api.tasks.models import FilterVerdict, JobClosedVerdict
    from core.filters import build_custom_instructions
    from core.pittcsc_simplify import (
        CLEARANCE_INSTRUCTIONS,
        CLOSED_INSTRUCTIONS,
        ClearanceRequirementResponse,
        JobClosedResponse,
    )

    job = db.query_one("SELECT id, url, company, title FROM jobs WHERE id = %s", (body.job_id,))
    if not job:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    filter_name = prompt_hash = None
    check = body.check
    if check == "closed":
        instructions = CLOSED_INSTRUCTIONS
        model_cls = JobClosedResponse if body.with_reason else JobClosedVerdict
        verdict_of = lambda p: (p.is_closed, getattr(p, "reason", "") or "")
    elif check == "clearance":
        instructions, model_cls = CLEARANCE_INSTRUCTIONS, ClearanceRequirementResponse
        verdict_of = lambda p: (
            p.requires_clearance_or_restrictions,
            p.reason or (p.restriction_type or ""),
        )
    elif check.startswith(("filter:", "hash:")):
        if check.startswith("hash:"):
            # Verdicts are cached by prompt_hash, not by filter id — several
            # users' filters can share one hash. Any of them reproduces the
            # same check, so re-running by hash is the honest admin-side
            # addressing for a verdict row.
            flt = db.query_one(
                "SELECT user_id, name, prompt, on_ambiguous, prompt_hash FROM user_filters "
                "WHERE prompt_hash = %s ORDER BY id LIMIT 1",
                (check.split(":", 1)[1],),
            )
        else:
            flt = db.query_one(
                "SELECT user_id, name, prompt, on_ambiguous, prompt_hash FROM user_filters WHERE id = %s",
                (int(check.split(":", 1)[1]),),
            )
        if not flt:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
        instructions = build_custom_instructions(flt["prompt"], flt["on_ambiguous"])
        model_cls, verdict_of = FilterVerdict, (lambda p: (p.should_filter, p.reason))
        filter_name = f"user{flt['user_id']}:{flt['name']}"
        prompt_hash = flt["prompt_hash"]
        check = "custom"
    else:
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_CHECK",
                "message": "check must be closed, clearance, filter:<id>, or hash:<prompt_hash>",
            },
        )
    # Re-fetch: a recheck against cached text cannot discover that a posting
    # has since closed, which is usually the whole reason for asking.
    fresh, closure_signal = await _verdicts.refresh_content(
        job["url"], company=job["company"], job_title=job["title"], context="manual"
    )
    if fresh is None:
        gone = db.query_one(
            "SELECT status, reason FROM ai_queries WHERE url = %s AND check_type = 'closed' "
            "ORDER BY id DESC LIMIT 1",
            (job["url"],),
        )
        if closure_signal:
            return {
                "check": body.check,
                "status": "rejected",
                "reason": (gone or {}).get("reason", ""),
                "tokens": 0,
                "refetched": True,
                "closure_signal": closure_signal,
            }
        raise HTTPException(
            409,
            detail={"code": "NO_CONTENT", "message": "could not fetch this posting just now"},
        )
    content = {"input_content": fresh}
    key = ai.server_key("openai")
    if not key:
        raise HTTPException(503, detail={"code": "NO_SERVER_KEY", "message": "no server key"})
    cfg = ai.AIConfig(
        provider="openai",
        api_key=key,
        key_source="owner",
        model=ai.DEFAULT_OPENAI_MODEL,
        params={"reasoning_effort": "medium" if body.with_reason else "low"},
    )
    parsed, usage = await _verdicts.run_check(
        cfg,
        url=job["url"],
        check_type=check,
        instructions=instructions,
        input_text=content["input_content"][:60000],
        response_model=model_cls,
        verdict_of=verdict_of,
        company=job["company"],
        job_title=job["title"],
        filter_name=filter_name,
        prompt_hash=prompt_hash,
        context="manual",
    )
    if parsed is None:
        raise HTTPException(
            502,
            detail={
                "code": "NO_VERDICT",
                "message": "the model returned no usable answer; try again",
            },
        )
    rejected, reason = verdict_of(parsed)
    return {
        "check": body.check,
        "status": "rejected" if rejected else "passed",
        "reason": reason,
        "tokens": usage.get("total_tokens", 0),
    }


@router.get("/health")
def data_health(user: AuthedUser = Depends(require_admin)):
    """Open data-health alerts plus recently resolved ones, so an upstream
    break is something you're told about rather than something you discover."""
    return {
        # Nothing is suppressed any more: the detectors exclude backlog-sweep
        # rows individually (health.FRESH_CHECK_WINDOW) instead of switching a
        # whole detector off while the content backfill runs. Kept in the
        # response so a future suppression has somewhere to be reported —
        # a detector that is off and says nothing is indistinguishable from a
        # detector that sees nothing.
        "suppressed": [],
        # subject_kind says WHAT an alert's subject is - a source, a host, a
        # provider and user, a task kind. It is not the same thing across
        # detectors, and the dashboard linked all of them to the sources page,
        # which is correct for two of five. Annotated on read from the one map
        # in health.py rather than stored per row, so alerts already open get
        # the right answer without a backfill.
        "open": [
            {**a, "subject_kind": health.subject_kind_for(a["kind"])}
            for a in db.query(
                "SELECT * FROM health_alerts WHERE resolved_at IS NULL "
                "ORDER BY severity, last_seen DESC"
            )
        ],
        "recently_resolved": [
            {**a, "subject_kind": health.subject_kind_for(a["kind"])}
            for a in db.query(
                "SELECT * FROM health_alerts WHERE resolved_at > now() - interval '7 days' "
                "ORDER BY resolved_at DESC LIMIT 20"
            )
        ],
        "content_mix": db.query(
            """
            SELECT j.source,
                   COUNT(*) FILTER (WHERE q.reason = 'ats text') AS ats_text,
                   COUNT(*) FILTER (WHERE q.reason = 'scraped') AS scraped,
                   COUNT(*) AS total
            FROM ai_queries q JOIN jobs j ON j.url = q.url
            WHERE q.check_type = 'content'
              AND q.created_at > now() - interval '7 days'
            GROUP BY j.source ORDER BY total DESC
            """
        ),
    }


@router.post("/health/check")
async def run_health_check(user: AuthedUser = Depends(require_admin)):
    """Run the detectors now instead of waiting for the hourly task."""
    from api import health

    found = health.detect()
    fresh = health.record(found)
    return {"open": len(found), "new": len(fresh), "alerts": found}


@router.get("/failures")
def failure_breakdown(
    hours: int = 24,
    worker: str | None = None,
    host: str | None = None,
    user: AuthedUser = Depends(require_admin),
):
    """Failed checks pivoted by fleet host and URL host: one worker failing on
    hosts the others handle fine is the signature of an IP block. Pass worker
    and/or host to drill into the individual failures behind a pivot row."""
    hours = max(1, min(hours, 720))
    params: dict = {"hours": hours, "worker": worker, "host": host}
    rows = db.query(
        """
        SELECT COALESCE(worker, 'unknown') AS worker,
               substring(url from '//([^/]+)') AS host,
               check_type, COUNT(*) AS failures,
               MAX(created_at) AS last_failure
        FROM ai_queries
        WHERE status = 'failed'
          AND created_at > now() - make_interval(hours => %(hours)s)
          AND (%(worker)s::text IS NULL OR COALESCE(worker, 'unknown') = %(worker)s)
          AND (%(host)s::text IS NULL OR substring(url from '//([^/]+)') = %(host)s)
        GROUP BY 1, 2, 3 ORDER BY failures DESC LIMIT 100
        """,
        params,
    )
    items = []
    if worker or host:
        items = db.query(
            """
            SELECT id, created_at, url, check_type, company, job_title,
                   COALESCE(worker, 'unknown') AS worker, left(error, 300) AS error, reason
            FROM ai_queries
            WHERE status = 'failed'
              AND created_at > now() - make_interval(hours => %(hours)s)
              AND (%(worker)s::text IS NULL OR COALESCE(worker, 'unknown') = %(worker)s)
              AND (%(host)s::text IS NULL OR substring(url from '//([^/]+)') = %(host)s)
            ORDER BY id DESC LIMIT 200
            """,
            params,
        )
    return {"rows": rows, "items": items}


class IngestBody(BaseModel):
    sources: list[str] | None = None


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


@router.delete("/sources/{name}")
def delete_source(name: str, force: bool = False, user: AuthedUser = Depends(require_admin)):
    """Permanently remove a source. Refuses while anything still hangs off it —
    jobs would be orphaned into a source that no longer exists, and there is no
    undo — so emptiness is proven rather than assumed. force=true is the
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
    return {"ok": True, "deleted": name, "was_attached": attached}


@router.delete("/source-groups/{name}")
def delete_source_group(name: str, user: AuthedUser = Depends(require_admin)):
    row = db.query_one("DELETE FROM source_groups WHERE name = %s RETURNING name", (name,))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown group"})
    return {"ok": True, "deleted": name}


@router.patch("/sources/{name}")
def patch_source(name: str, body: SourceBody, user: AuthedUser = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True, exclude={"name"})
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    for k in ("company", "title_pattern"):
        if k in fields:
            fields[k] = (fields[k] or "").strip() or None
    current = db.query_one("SELECT * FROM sources WHERE name = %s", (name,))
    if not current:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    merged = {**current, **fields}
    _check_source(merged["listings_url"], merged["company"], merged["title_pattern"])
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    row = db.query_one(
        f"UPDATE sources SET {cols} WHERE name = %(name)s RETURNING *",
        {"name": name, **fields},
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown source"})
    return row


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
def switch_sources(body: SourceSwitchBody, user: AuthedUser = Depends(require_admin)):
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
    from core import boards

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
    return {
        "active": body.active,
        "ingest_interval_hours": body.ingest_interval_hours,
        "selected": sorted(selected),
        "changed": sorted(r["name"] for r in changed),
    }


# key -> the type its value must have. Was a bare set of names back when
# every config value was a bool; the mailbox gate is a list of group names, so
# the endpoint validates per key rather than assuming one shape for all.
_CONFIG_KEYS: dict[str, type] = {
    "signups_enabled": bool,
    "gmail_connect_groups": list,
    # Hours a posting whose page fetch came back empty waits before any ingest
    # or backfill tries it again. Read by api.tasks.board.fetch_retry_interval.
    "fetch_retry_after_hours": int,
}


@router.get("/config")
def get_config(user: AuthedUser = Depends(require_admin)):
    rows = db.query("SELECT key, value FROM app_config ORDER BY key")
    return {"config": {r["key"]: r["value"] for r in rows}}


class ConfigPut(BaseModel):
    value: bool | int | list[str]


@router.put("/config/{key}")
def put_config(key: str, body: ConfigPut, user: AuthedUser = Depends(require_admin)):
    expected = _CONFIG_KEYS.get(key)
    if expected is None:
        raise HTTPException(
            400,
            detail={"code": "UNKNOWN_KEY", "message": f"key must be one of {sorted(_CONFIG_KEYS)}"},
        )
    # bool is an int to isinstance, so an int key checks the exact type and
    # refuses zero and below: a retry window of 0 hours is the hourly
    # hammering this key exists to stop.
    if expected is int:
        if type(body.value) is not int or body.value < 1:
            raise HTTPException(
                400,
                detail={
                    "code": "INVALID_VALUE",
                    "message": f"{key} takes a whole number of 1 or more",
                },
            )
    elif not isinstance(body.value, expected):
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_VALUE",
                "message": f"{key} takes a {expected.__name__}",
            },
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
    total_row = db.query_one(f"SELECT COUNT(*) AS c FROM reports r {where}", {"status": status})
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
    total = total_row["c"] if total_row else 0
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
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
    weekly_token_budget: int | None = Field(default=None, ge=0)
    allowed_models: list[str] | None = Field(default=None, max_length=50)


@router.get("/group-budgets")
def list_group_budgets(user: AuthedUser = Depends(require_admin)):
    from api import ai

    return {
        "groups": db.query(
            "SELECT group_name, weekly_token_budget, allowed_models "
            "FROM group_budgets ORDER BY group_name"
        ),
        "catalog_models": sorted(
            m["model"] for models in ai.MODEL_CATALOG.values() for m in models
        ),
    }


@router.put("/group-budgets/{group_name}")
def put_group_budget(
    group_name: str, body: GroupBudgetPut, user: AuthedUser = Depends(require_admin)
):
    from api import ai

    if body.allowed_models is not None:
        known = {m["model"] for models in ai.MODEL_CATALOG.values() for m in models}
        unknown = [m for m in body.allowed_models if m not in known]
        if unknown:
            raise HTTPException(
                400,
                detail={"code": "UNKNOWN_MODEL", "message": f"unknown models: {unknown}"},
            )
    db.execute(
        """
        INSERT INTO group_budgets (group_name, weekly_token_budget, allowed_models)
        VALUES (%s, %s, %s)
        ON CONFLICT (group_name) DO UPDATE SET
            weekly_token_budget = EXCLUDED.weekly_token_budget,
            allowed_models = EXCLUDED.allowed_models
        """,
        (group_name, body.weekly_token_budget, body.allowed_models),
    )
    return {
        "group_name": group_name,
        "weekly_token_budget": body.weekly_token_budget,
        "allowed_models": body.allowed_models,
    }


@router.delete("/group-budgets/{group_name}")
def delete_group_budget(group_name: str, user: AuthedUser = Depends(require_admin)):
    db.execute("DELETE FROM group_budgets WHERE group_name = %s", (group_name,))
    return {"ok": True}


@router.get("/queries")
def list_queries(
    check_type: str | None = None,
    status: str | None = None,
    config: str | None = None,
    url: str | None = None,
    q: str | None = None,
    deep: bool = False,
    sources: str | None = None,
    reason_group: str | None = None,
    evidence_missing: bool = False,
    prompt_hash: str | None = None,
    sort: str = "id",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
):
    try:
        where, params = _where(
            check_type,
            status,
            config,
            url,
            q,
            deep,
            sources,
            reason_group,
            evidence_missing,
            prompt_hash,
        )
    except KeyError:
        # An unknown group key is a bad request, not a server fault. The keys
        # are a closed server-side vocabulary, so a caller sending one that
        # does not exist has a stale link, and should be told which.
        raise HTTPException(
            400,
            detail={
                "code": "UNKNOWN_REASON_GROUP",
                "message": f"unknown reason_group: {reason_group}",
                "valid": [g.key for g in reason_taxonomy.GROUPS],
            },
        ) from None
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
    total = total_row["c"] if total_row else 0
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
    }


@router.get("/queries/{query_id}")
def get_query(query_id: int, user: AuthedUser = Depends(require_admin)):
    row = db.query_one("SELECT * FROM ai_queries WHERE id = %s", (query_id,))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown query"})
    return row


class DeleteQueries(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=10000)


@router.post("/queries/delete")
def delete_queries(body: DeleteQueries, user: AuthedUser = Depends(require_admin)):
    with db.pool.connection() as conn:
        result = conn.execute("DELETE FROM ai_queries WHERE id = ANY(%s)", (body.ids,))
        deleted = result.rowcount
    return {"deleted": deleted}


# Sortable columns for the job aggregate. Values are looked up here, never
# interpolated from the request, which is what keeps the ORDER BY safe.
_JOBS_SORTABLE = {
    "last_seen": "last_seen",
    "company": "lower(MAX(company))",
    "job_title": "lower(MAX(job_title))",
    "checks": "checks",
    "passed": "passed",
    "rejected": "rejected",
    "failed": "failed",
    "total_tokens": "total_tokens",
    "url": "url",
}


@router.get("/jobs")
def list_jobs(
    q: str | None = None,
    config: str | None = None,
    verdict: str | None = None,
    sources: str | None = None,
    sort: str = "last_seen",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
):
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    sub = ["url IS NOT NULL"]
    params: dict = {}
    wanted_sources = [s.strip() for s in (sources or "").split(",") if s.strip()]
    if wanted_sources:
        # Same shape as _where(): ai_queries is keyed by url and source lives on
        # the job, so a subquery keeps this composable with the count and page
        # queries below rather than making every other filter ambiguous.
        sub.append("url IN (SELECT url FROM jobs WHERE source = ANY(%(sources)s))")
        params["sources"] = wanted_sources
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
        WHERE url IN (SELECT url FROM ai_queries WHERE {" AND ".join(sub)})
        GROUP BY url
        {having}
    """
    total_row = db.query_one(f"SELECT COUNT(*) AS c FROM ({base}) sub", params)
    sort_col = _JOBS_SORTABLE.get(sort, "last_seen")
    direction = "ASC" if dir == "asc" else "DESC"
    rows = db.query(
        f"{base} ORDER BY {sort_col} {direction} NULLS LAST, url LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    )
    for r in rows:
        r["verdict"] = "rejected" if r["rejected"] > 0 else "passed" if r["passed"] > 0 else "other"
    total = total_row["c"] if total_row else 0
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
        # Echoed so the UI can render the active sort without duplicating the
        # default, and sortable so it never has to guess the accepted keys.
        "sort": sort if sort in _JOBS_SORTABLE else "last_seen",
        "dir": direction.lower(),
        "sortable": sorted(_JOBS_SORTABLE),
    }


@router.get("/jobs/responses")
def job_responses(url: str, user: AuthedUser = Depends(require_admin)):
    return {"rows": db.query("SELECT * FROM ai_queries WHERE url = %s ORDER BY id ASC", (url,))}


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
    def distinct(col: str) -> list[str]:
        return [
            r["v"]
            for r in db.query(
                f"SELECT DISTINCT {col} AS v FROM ai_queries WHERE {col} IS NOT NULL ORDER BY {col}"
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
        -- created_at is timestamptz since a7c1e9d40b22; substr() has no
        -- overload for it. ::date also makes the bucket a real calendar day
        -- in the session timezone rather than the first ten characters of
        -- whatever string the writer happened to produce.
        SELECT created_at::date AS day,
               COUNT(*) AS queries,
               COUNT(*) FILTER (WHERE status = 'failed') AS failed,
               COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
        FROM ai_queries GROUP BY day ORDER BY day ASC
        """
    )
    # Cost is computed here, not in the browser. The client had one hardcoded
    # gpt-5-nano price applied to every token, but PRICES_PER_MTOK spans
    # $0.05-$5.00 per Mtok - a 100x range - so the headline number was wrong
    # the moment anything ran on a different model, and silently so.
    # Batched calls bill at half price, which the client could not know either.
    by_model = db.query(
        """
        SELECT model,
               COUNT(*) AS queries,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(prompt_tokens) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_prompt_tokens,
               COALESCE(SUM(completion_tokens) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_completion_tokens,
               COALESCE(SUM(cached_tokens) FILTER (WHERE batch_id IS NOT NULL), 0) AS batched_cached_tokens
        FROM ai_queries WHERE model IS NOT NULL GROUP BY model ORDER BY queries DESC
        """
    )
    total_cost = Decimal(0)
    for row in by_model:
        if pricing.is_tiered(row["model"]):
            # This prices SUMMED tokens, and a tiered model's rate depends on
            # each individual request's prompt length. A thousand small calls
            # sum into a tier none of them was billed at, so the only honest
            # answer here is "not priced" - the same NULL an unknown model
            # gets. Per-row cost_usd is written at call time and is the right
            # source for a total once this endpoint sums that instead.
            row["cost_usd"] = None
            continue
        # Batched and synchronous tokens bill at different rates, so they are
        # priced as two separate calls and summed - one blended rate over the
        # whole model would be wrong by up to 2x depending on the mix.
        batched = pricing.estimate_cost_usd(
            row["model"],
            row["batched_prompt_tokens"],
            row["batched_completion_tokens"],
            cached_tokens=row["batched_cached_tokens"],
            batched=True,
        )
        sync = pricing.estimate_cost_usd(
            row["model"],
            int(row["prompt_tokens"]) - int(row["batched_prompt_tokens"]),
            int(row["completion_tokens"]) - int(row["batched_completion_tokens"]),
            cached_tokens=int(row["cached_tokens"]) - int(row["batched_cached_tokens"]),
        )
        if batched is None or sync is None:
            row["cost_usd"] = None
            continue
        cost = batched + sync
        row["cost_usd"] = round(float(cost), 6)
        total_cost += cost
    if totals is not None:
        totals["cost_usd"] = round(float(total_cost), 6)

    return {
        "totals": totals,
        "by_check_type": by_check_type,
        "by_status": by_status,
        "by_day": by_day,
        "by_model": by_model,
    }
