"""The mail pipeline, visible enough to correct.

Mirrors /admin/queries deliberately: filter, sort, drill in, see what the model
actually said. That surface is the one people trust in this codebase, and a
pipeline whose decisions cannot be inspected is one whose mistakes are found
by noticing a wrong answer months later.

Every decision here is reversible by design, which is what makes an override
endpoint honest rather than a patch: classifications and matches are both
append-only, so a correction is a newer row, not an edit.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import db, mail_match, mail_pipeline
from api.auth import AuthedUser, require_user
from api.routers.admin import require_admin

router = APIRouter()

_SORTABLE = {
    "sent_at": "m.sent_at",
    "imported_at": "m.imported_at",
    "id": "m.id",
}


def _where(
    *,
    kind: str | None,
    matched: bool | None,
    source: str | None,
    prefilter: bool | None,
    q: str | None,
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if kind:
        clauses.append("ev.kind = %(kind)s")
        params["kind"] = kind
    if matched is not None:
        # NULL application_id is a real recorded outcome - "we looked and
        # found nothing" - so unmatched is a value to filter on, not an
        # absence to skip over.
        clauses.append("mt.application_id IS NOT NULL" if matched else "mt.application_id IS NULL")
    if source:
        clauses.append("m.source = %(source)s")
        params["source"] = source
    if prefilter is not None:
        clauses.append("COALESCE(m.prefilter_hit, FALSE) = %(prefilter)s")
        params["prefilter"] = prefilter
    if q:
        clauses.append("(m.subject ILIKE %(q)s OR m.from_email ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


# Latest row per message for both, because both logs are append-only and only
# the newest verdict counts. Spelled once here so the list and the detail view
# cannot disagree about what "current" means.
_CURRENT = """
    LEFT JOIN LATERAL (
        SELECT kind, confidence, deadline_at, deadline_inferred, detail, model, id
        FROM email_events WHERE message_id = m.id ORDER BY id DESC LIMIT 1
    ) ev ON TRUE
    LEFT JOIN LATERAL (
        SELECT application_id, method, confidence AS match_confidence, rationale, id AS match_id
        FROM application_matches WHERE message_id = m.id ORDER BY id DESC LIMIT 1
    ) mt ON TRUE
"""


@router.get("/admin/mail")
def list_mail(
    kind: str | None = None,
    matched: bool | None = None,
    source: str | None = None,
    prefilter: bool | None = None,
    q: str | None = None,
    sort: str = "sent_at",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
):
    where, params = _where(kind=kind, matched=matched, source=source, prefilter=prefilter, q=q)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    total = db.query_one(
        f"SELECT COUNT(*) AS c FROM email_messages m {_CURRENT} WHERE TRUE {where}", params
    )
    order = _SORTABLE.get(sort, "m.sent_at")
    direction = "ASC" if dir == "asc" else "DESC"
    rows = db.query(
        f"""
        SELECT m.id, m.provider_message_id, m.source, m.from_email, m.subject, m.sent_at,
               m.prefilter_hit, m.prefilter_reason,
               ev.kind, ev.confidence, ev.deadline_at, ev.deadline_inferred, ev.model,
               mt.application_id, mt.method, mt.match_confidence, mt.rationale,
               a.company_name, a.title
        FROM email_messages m
        {_CURRENT}
        LEFT JOIN applications a ON a.id = mt.application_id
        WHERE TRUE {where}
        ORDER BY {order} {direction} NULLS LAST, m.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    )
    return {
        "rows": rows,
        "total": total["c"] if total else 0,
        "page": page,
        "page_size": page_size,
    }


@router.get("/admin/mail/{message_id}")
def mail_detail(message_id: int, user: AuthedUser = Depends(require_admin)):
    """One message with its FULL history, not just the current verdict.

    Every classification and every match attempt, oldest first. That history
    is the point: a match that changed when a posting finally reached the
    board, or a classification corrected on a later pass, is exactly what
    someone debugging a wrong answer needs to see.
    """
    message = db.query_one("SELECT * FROM email_messages WHERE id = %s", (message_id,))
    if not message:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown message"})
    return {
        "message": message,
        "events": db.query(
            "SELECT * FROM email_events WHERE message_id = %s ORDER BY id", (message_id,)
        ),
        "matches": db.query(
            """
            SELECT am.*, a.company_name, a.title, a.job_id
            FROM application_matches am
            LEFT JOIN applications a ON a.id = am.application_id
            WHERE am.message_id = %s ORDER BY am.id
            """,
            (message_id,),
        ),
        # What tier 1 would see, so a missed exact-link match can be diagnosed
        # without re-running the matcher and guessing at why.
        "canonical_urls": sorted(mail_match.canonical_urls(message.get("body_text"))),
    }


class MatchOverride(BaseModel):
    application_id: int | None


@router.post("/admin/mail/{message_id}/match")
def override_match(message_id: int, body: MatchOverride, user: AuthedUser = Depends(require_admin)):
    """Correct a match by hand.

    An append, not an edit: the matcher's own attempt survives underneath, so
    a systematically wrong tier stays visible in the history instead of being
    quietly papered over one row at a time. That history is the only evidence
    that the matcher needs fixing rather than the row.
    """
    message = db.query_one("SELECT user_id FROM email_messages WHERE id = %s", (message_id,))
    if not message:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown message"})
    if body.application_id is not None:
        owner = db.query_one(
            "SELECT user_id FROM applications WHERE id = %s", (body.application_id,)
        )
        if not owner or owner["user_id"] != message["user_id"]:
            # Cross-user match would attribute one person's outcome to
            # another's application. 404 rather than 403: whether that
            # application exists is not something the caller is entitled to.
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown application"})
    mail_match.record(
        message_id,
        mail_match.Match(
            body.application_id,
            "manual",
            "high" if body.application_id is not None else "none",
            f"set by admin {user.sub}",
        ),
    )
    if body.application_id is not None:
        mail_pipeline.sync_action_items(body.application_id)
    return {"ok": True, "current": mail_match.latest(message_id)}


@router.get("/user/pipeline")
def pipeline(
    include_closed: bool = Query(default=False),
    user: AuthedUser = Depends(require_user),
):
    """The user's applications with their derived stage and open actions."""
    apps = db.query(
        "SELECT id, job_id, company_name, title, applied_at FROM applications "
        "WHERE user_id = %s ORDER BY applied_at DESC NULLS LAST, id DESC",
        (user.id,),
    )
    out = []
    for app in apps:
        state = mail_pipeline.state_of(app["id"])
        if not include_closed and state["stage"] in mail_pipeline.TERMINAL:
            continue
        out.append({**app, **state})
    return {
        "applications": out,
        "actions": db.query(
            """
            SELECT ai.*, a.company_name, a.title
            FROM action_items ai LEFT JOIN applications a ON a.id = ai.application_id
            WHERE ai.user_id = %s AND ai.resolved_at IS NULL
            ORDER BY ai.due_at NULLS LAST, ai.id
            """,
            (user.id,),
        ),
    }
