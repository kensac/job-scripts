from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import db, events, signals, sorting, visibility
from api.auth import AuthedUser, require_user
from api.models import UploadRequest, UserJobPatch, UserJobsBulkIds, UserJobsBulkPatch
from core.urls import normalize_url

router = APIRouter()

# closed_verdict is three-valued and must stay that way: 'open', 'closed', or
# NULL for never checked. `active` cannot answer this question - it is whatever
# a board's feed last said and nothing ever clears it, so on a board that is
# not re-listed (sheet_import, imported once from a sheet) it reports our own
# stale copy as the posting's state. The closed check observes the posting
# itself and is applied uniformly across boards, which is what makes it
# comparable. Collapsing NULL into 'closed' would reintroduce exactly the bug
# this column exists to fix: 114 of the applications flagged dead by `active`
# have a closed-check that says the posting is open.
_JOB_ROW = """
    j.id AS job_id, j.company, j.title, j.locations, j.terms, j.source,
    j.url, j.raw_url, j.active, j.date_posted, j.created_at AS added_at,
    j.extraction_status, j.comp_min, j.comp_max, j.comp_text, j.comp_currency,
    j.comp_period, j.comp_basis,
    (SELECT CASE q.status WHEN 'passed' THEN 'open' WHEN 'rejected' THEN 'closed' END
     FROM ai_queries q
     WHERE q.url = j.url AND q.check_type = 'closed' AND q.status IN ('passed', 'rejected')
     ORDER BY q.id DESC LIMIT 1) AS closed_verdict,
    uj.status, uj.date_applied, uj.notes, uj.size, uj.recruiter,
    uj.connection1, uj.connection2, uj.documents,
    COALESCE(uj.hidden, FALSE) AS hidden
"""

_VISIBILITY = visibility.FULL


def _visible_job(user: AuthedUser, job_id: int, columns: str) -> dict | None:
    """One job, but only if this user may address it.

    Every per-job route needs this and none of them had it: they resolved the
    job with a bare `WHERE id = %s`, so any signed-in user could name any of
    the 49k job ids. That let them read another user's private upload, pin it
    to their own board, and - through the explain route, which writes a verdict
    into an append-only log with no user_id - flip a job's closed status for
    EVERY user at once, because latest-row-per-(url, check_type) wins globally.

    The gate is the board's own membership (api.visibility.FAST) rather than
    a new predicate. A fourth spelling of "can this user see this job" is how
    the first three drifted.
    """
    return db.query_one(
        visibility.FAST.format(columns=columns, extra="AND j.id = %(jid)s"),
        {"uid": user.id, "jid": job_id},
    )


def _require_visible_job(user: AuthedUser, job_id: int, columns: str) -> dict:
    job = _visible_job(user, job_id, columns)
    if not job:
        # 404, not 403: whether a job exists is itself information the caller
        # is not entitled to.
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    return job


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

# What a status MEANS, served beside the names so the board never decides
# "is this over" or "which tone" by matching a name it hand-copied: a status
# absent here is in play with no outcome. The withdrawn pair mirrors
# mail_pipeline.WITHDRAWN_STATUSES.
_STATUS_META: dict[str, tuple[bool, str | None]] = {
    "Accepted": (True, "won"),
    "Rejected": (True, "lost"),
    "No Longer Interested": (True, "withdrawn"),
    "Withdrawn": (True, "withdrawn"),
}


def status_meta(statuses: list[str]) -> list[dict[str, Any]]:
    return [
        {"name": name, "terminal": meta[0], "outcome": meta[1]}
        for name in statuses
        for meta in (_STATUS_META.get(name, (False, None)),)
    ]


REPORT_KINDS = ("stale", "wrong_data", "closed", "other")
_REPORT_LABELS = {
    "stale": "Posting is stale",
    "wrong_data": "Details are wrong",
    "closed": "Posting is closed",
    "other": "Something else",
}


def report_kinds() -> list[dict[str, str]]:
    """The kinds a report can carry, with the label the form shows; one copy,
    served to the board's report modal and the admin reports page."""
    return [{"kind": k, "label": _REPORT_LABELS[k]} for k in REPORT_KINDS]


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
    return {
        "statuses": statuses,
        "status_meta": status_meta(statuses),
        "not_applied_sentinel": NOT_APPLIED,
        "sources": sources,
        "report_kinds": report_kinds(),
    }


@router.get("/user/jobs")
def list_jobs(
    limit: int = 200,
    offset: int = 0,
    cursor: int | None = None,
    sort: str = "added_at",
    dir: str = "desc",
    search: str | None = None,
    status: str | None = None,
    statuses: str | None = None,
    source: str | None = None,
    include_hidden: bool = False,
    with_total: bool = False,
    user: AuthedUser = Depends(require_user),
):
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    sorts = sorting.parse(sort, dir, _SORTABLE, "added_at")
    extra = []
    params: dict = {"uid": user.id, "limit": limit + 1, "offset": offset}
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
    if cursor is not None:
        # Legacy cursor mode: fixed newest-first by id.
        order = "AND j.id < %(cursor)s\nORDER BY j.id DESC LIMIT %(limit)s"
        params["cursor"] = cursor
    else:
        order = (
            f"ORDER BY {sorting.clause(sorts, _SORTABLE)}, j.id DESC "
            "LIMIT %(limit)s OFFSET %(offset)s"
        )
    # One pass, not two: the total rides on the page as a window count over
    # the same filtered set, so a sort with with_total costs one board read
    # rather than the count query and then the page query.
    columns = _JOB_ROW + (", COUNT(*) OVER () AS total_rows" if with_total else "")
    sql = visibility.FAST.format(columns=columns, extra=f"{filter_sql}\n{order}")
    rows = db.query(sql, params)
    if with_total:
        if rows:
            total = rows[0]["total_rows"]
            for r in rows:
                r.pop("total_rows", None)
        else:
            # A page past the end carries no row to read the count from.
            row = db.query_one(
                visibility.FAST.format(columns="COUNT(*) AS c", extra=filter_sql), params
            )
            total = row["c"] if row else 0
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "rows": rows,
        "next_cursor": rows[-1]["job_id"] if cursor is not None and has_more and rows else None,
        "has_more": has_more,
        "offset": offset,
        "total": total,
        # The active sort as applied, so the UI renders it without duplicating
        # the default, and the keys it may ask for.
        "sorts": sorts,
        "sortable": sorted(_SORTABLE),
        # When the board's membership was last computed, so a page can say
        # "as of" and a person knows a preference change has landed.
        "board_computed_at": visibility.computed_at(user.id),
    }


def _touchable(user: AuthedUser, job_ids: list[int]) -> set[int]:
    """The ids this user may write a board row for.

    Pinning an unsubscribed job by patching it is a deliberate feature (the
    "watching" case). But a user_jobs row IS a visibility grant - _VISIBILITY
    trusts a row the person acted on unconditionally - so an unrestricted pin
    launders around every other gate: pin, then read the job's cached page
    through /detail. The public catalog is fine to pin; another user's
    private upload is not, and that is the only distinction that matters.
    """
    rows = db.query(
        "SELECT id FROM jobs WHERE id = ANY(%s) AND (uploaded_by IS NULL OR uploaded_by = %s)",
        (job_ids, user.id),
    )
    return {r["id"] for r in rows}


def _write_board_row(user_id: int, job_id: int, patch: dict) -> dict:
    """Applies one patch to one board row; returns what it filled in itself."""
    fields = dict(patch)
    autofilled = {}
    existing = None
    if "status" in fields or "date_applied" not in fields:
        existing = db.query_one(
            "SELECT status, date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
    # Setting any real status implies the user acted on the job; stamp
    # date_applied once so they never have to fill it by hand.
    if fields.get("status") and "date_applied" not in fields:
        if not existing or existing["date_applied"] is None:
            # UTC, not the container's local date. The containers run
            # TZ=America/New_York, so date.today() silently decided
            # "today" in Eastern for every user regardless of theirs.
            fields["date_applied"] = datetime.datetime.now(datetime.UTC).date()
            autofilled["date_applied"] = fields["date_applied"].isoformat()
    if "status" in fields:
        old_status = existing["status"] if existing else None
        if (old_status or "") != (fields["status"] or ""):
            db.execute(
                "INSERT INTO user_job_history (user_id, job_id, old_status, new_status) "
                "VALUES (%s, %s, %s, %s)",
                (user_id, job_id, old_status, fields["status"]),
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
        {"uid": user_id, "jid": job_id, **fields},
    )
    return autofilled


def _patch_fields(body: UserJobPatch) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    return fields


@router.patch("/user/jobs/{job_id}")
def patch_job(job_id: int, body: UserJobPatch, user: AuthedUser = Depends(require_user)):
    if job_id not in _touchable(user, [job_id]):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown job"})
    fields = _patch_fields(body)
    return {"ok": True, "autofilled": _write_board_row(user.id, job_id, fields)}


@router.patch("/user/jobs")
def patch_jobs(body: UserJobsBulkPatch, user: AuthedUser = Depends(require_user)):
    """One patch across a selection, so a 6,000-row selection is one request
    rather than 6,000. Ids the caller may not touch are skipped and named,
    not refused whole: a selection made from the board can include a row that
    vanished or was never theirs, and failing everything for it would send
    the page back to one request per row."""
    fields = _patch_fields(body.patch)
    allowed = _touchable(user, body.job_ids)
    updated = 0
    for job_id in body.job_ids:
        if job_id in allowed:
            _write_board_row(user.id, job_id, fields)
            updated += 1
    return {
        "ok": True,
        "updated": updated,
        "skipped": [j for j in body.job_ids if j not in allowed],
    }


@router.get("/user/jobs/{job_id}/detail")
def job_detail(job_id: int, user: AuthedUser = Depends(require_user)):
    """Everything behind one board row: cached posting content, the user's own
    row + status history, and why the AI let it through (per-filter verdicts
    plus the closed/clearance checks)."""
    job = _require_visible_job(
        user,
        job_id,
        "j.id, j.url, j.raw_url, j.company, j.title, j.locations, j.terms, j.source, "
        "j.active, j.date_posted, j.comp_min, j.comp_max, j.comp_text, j.comp_currency, "
        "j.comp_period, j.comp_basis, "
        "j.created_at, "
        "(SELECT CASE q.status WHEN 'passed' THEN 'open' WHEN 'rejected' THEN 'closed' END "
        " FROM ai_queries q WHERE q.url = j.url AND q.check_type = 'closed' "
        " AND q.status IN ('passed', 'rejected') ORDER BY q.id DESC LIMIT 1) AS closed_verdict",
    )
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
        # Every key is optional and absence means the signal does not exist -
        # never a zero, never something for the caller to re-derive.
        "signals": signals.signals_for(job),
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
async def explain_check(job_id: int, body: ExplainBody, user: AuthedUser = Depends(require_user)):
    """On-demand debugging: re-runs one check with the reason-ful schema and
    fuller reasoning (default verdicts skip reasons to save output tokens).
    Records a fresh verdict row (context 'explain') and returns the reason."""
    import dataclasses

    from api import budget
    from api import verdicts as _verdicts
    from api.tasks.models import FilterVerdict
    from core.filters import build_custom_instructions
    from core.pittcsc_simplify import (
        CLEARANCE_INSTRUCTIONS,
        CLOSED_INSTRUCTIONS,
        ClearanceRequirementResponse,
        JobClosedResponse,
    )

    # This route writes a verdict into ai_queries, which has no user_id and is
    # resolved latest-row-per-(url, check_type) for EVERY user. An ungated
    # job_id here is therefore not a read leak but a write primitive against
    # everyone's board.
    job = _require_visible_job(user, job_id, "j.id, j.url, j.company, j.title")
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
                "check": body.check,
                "status": "rejected",
                "reason": (gone or {}).get("reason", ""),
                "refetched": True,
                "closure_signal": closure_signal,
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
            detail={
                "code": "INVALID_CHECK",
                "message": "check must be closed, clearance, or filter:<id>",
            },
        )

    parsed, usage = await _verdicts.run_check(
        cfg,
        url=job["url"],
        check_type=check,
        instructions=instructions,
        input_text=content_row["input_content"][:60000],
        response_model=model_cls,
        verdict_of=verdict_of,
        company=job["company"],
        job_title=job["title"],
        filter_name=filter_name,
        prompt_hash=prompt_hash,
        context="explain",
    )
    budget.record_usage(
        user.id,
        cfg.key_source,
        "explain",
        cfg.model,
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        usage.get("total_tokens", 0),
    )
    if parsed is None:
        # run_check records the 'failed' row and returns None when the model
        # produces no parseable output; the tokens are already spent, so this
        # must read as a real outcome rather than an unhandled AttributeError.
        raise HTTPException(
            502,
            detail={
                "code": "NO_VERDICT",
                "message": "the model returned no usable answer; try again",
            },
        )
    rejected, reason = verdict_of(parsed)
    return {"check": body.check, "status": "rejected" if rejected else "passed", "reason": reason}


@router.delete("/user/jobs/{job_id}")
def delete_user_job(job_id: int, user: AuthedUser = Depends(require_user)):
    """Drops the user's board row only (the catalog job is untouched); a later
    run re-materializes it if it still passes their filters. Hide is the
    permanent alternative."""
    db.execute("DELETE FROM user_jobs WHERE user_id = %s AND job_id = %s", (user.id, job_id))
    return {"ok": True}


@router.delete("/user/jobs")
def delete_user_jobs(body: UserJobsBulkIds, user: AuthedUser = Depends(require_user)):
    """The selection form of the delete above: the caller's own rows only,
    one statement, count returned."""
    deleted = db.execute_count(
        "DELETE FROM user_jobs WHERE user_id = %s AND job_id = ANY(%s)", (user.id, body.job_ids)
    )
    return {"ok": True, "deleted": deleted}


@router.post("/uploads")
def upload_links(body: UploadRequest, user: AuthedUser = Depends(require_user)):
    from api import ssrf

    accepted = []
    rejected = []
    for submitted in body.urls:
        raw = submitted.strip()
        if not raw.startswith(("http://", "https://")):
            continue
        # Fail here as well as in the fetcher: an upload is the one place a
        # user chooses the URL, and rejecting it now gives them an answer
        # instead of a job that silently never extracts.
        error = ssrf.validate_public_url(raw)
        if error:
            rejected.append({"url": raw, "error": error})
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
    return {"accepted": accepted, "rejected": rejected}


class ReportBody(BaseModel):
    kind: str
    message: str = ""
    corrections: dict | None = None


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
    # Task ids are sequential and `error` is str(exc) written verbatim by the
    # worker, so an ungated lookup hands any signed-in user every other user's
    # failures. Ownership lives in the payload: every user-initiated kind
    # stamps user_id there, and the kinds that do not (ingest_source,
    # verify_new, data_health...) are fleet work with no user to show it to.
    row = db.query_one(
        "SELECT id, kind, status, progress, error, created_at, started_at, finished_at "
        "FROM tasks WHERE id = %s AND (payload->>'user_id')::bigint = %s",
        (task_id, user.id),
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown task"})
    return row
