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


# Strongest evidence first. A tier is a property of a MATCH, not of an
# application - matches are append-only and a message can be rematched - so a
# row reports the strongest tier among its current matches and says so.
_TIER_STRENGTH = (
    mail_match.EXACT_LINK,
    mail_match.ATS_COMPANY,
    mail_match.COMPANY_TITLE,
    mail_match.ADJUDICATED,
    "derived",
)


def _tiers_by_application(user_id: int) -> dict[int, dict[str, str]]:
    rows = db.query(
        """
        WITH current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id, method, confidence
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT cm.application_id, cm.method, cm.confidence
        FROM current_match cm JOIN applications a ON a.id = cm.application_id
        WHERE a.user_id = %s AND cm.application_id IS NOT NULL
        """,
        (user_id,),
    )
    best: dict[int, tuple[int, str, str]] = {}
    for row in rows:
        app_id = row["application_id"]
        rank = _TIER_STRENGTH.index(row["method"]) if row["method"] in _TIER_STRENGTH else 99
        if app_id not in best or rank < best[app_id][0]:
            best[app_id] = (rank, row["method"], row["confidence"])
    return {
        app_id: {"strongest_tier": method, "tier_confidence": confidence}
        for app_id, (_, method, confidence) in best.items()
    }


def _rows_for(user_id: int) -> list[dict[str, Any]]:
    """Every application with its derived stage. One query for applications,
    one for all their events - not two per application."""
    apps = db.query(
        "SELECT id, job_id, company_name, title, applied_at, source_provenance, "
        "dismissed_at, dismissed_reason FROM applications WHERE user_id = %s",
        (user_id,),
    )
    events = mail_pipeline.events_by_application(user_id)
    tiers = _tiers_by_application(user_id)
    # Derived at read time and attached rather than stored, so it moves when
    # core.ats learns a provider or when a sender turns out to serve more
    # companies than it did when the match was made.
    senders = mail_pipeline.sender_signal(user_id)
    out = []
    for app in apps:
        own = events.get(app["id"], [])
        out.append(
            {
                **app,
                "stage": mail_pipeline.stage_for(own),
                "event_count": len(own),
                "last_event_at": max((e["sent_at"] for e in own if e["sent_at"]), default=None),
                **tiers.get(app["id"], {"strongest_tier": None, "tier_confidence": None}),
                # A possibility, not a verdict: nothing filters on this and no
                # application is hidden by it. It says "worth confirming", and
                # carries the evidence so the reader can disagree.
                "sender": senders.get(app["id"]),
            }
        )
    return out


@router.get("/user/pipeline/summary")
def pipeline_summary(user: AuthedUser = Depends(require_user)):
    """Stage counts, derived by the same code that derives the list.

    An endpoint rather than a client-side sum: the moment the browser
    aggregates, the page owns a derivation the server owns, and the two drift
    the first time either changes. Counts, not a funnel - terminal stages beat
    progress regardless of arrival order and a stage is recomputed rather than
    advanced, so nothing flows through anything.
    """
    rows = _rows_for(user.id)
    live = [r for r in rows if r["dismissed_at"] is None]
    counts: dict[str, int] = {}
    for row in live:
        counts[row["stage"]] = counts.get(row["stage"], 0) + 1
    with_evidence = sum(1 for r in live if r["event_count"])
    # Dismissals are counted, not hidden. They change every other number here,
    # and a total that silently shrinks week to week with nothing explaining
    # why is the failure this whole system keeps producing.
    return {
        "counts": counts,
        "total": len(live),
        "with_evidence": with_evidence,
        "without_evidence": len(live) - with_evidence,
        "dismissed": len(rows) - len(live),
    }


@router.get("/user/pipeline")
def pipeline(
    include_closed: bool = Query(default=False),
    stage: str | None = Query(default=None),
    provenance: str | None = Query(default=None),
    tier: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
):
    """The user's applications with their derived stage and open actions.

    Stage is derived, so it cannot be filtered or paged in SQL. Both happen
    after derivation, which is why the bulk events query matters: the
    alternative is deriving a page's worth and having no idea what the totals
    are.
    """
    rows = _rows_for(user.id)
    if stage == "dismissed":
        rows = [r for r in rows if r["dismissed_at"] is not None]
        stage = None
    else:
        rows = [r for r in rows if r["dismissed_at"] is None]
    if not include_closed and not stage:
        rows = [r for r in rows if r["stage"] not in mail_pipeline.TERMINAL]
    if stage:
        rows = [r for r in rows if r["stage"] == stage]
    if provenance:
        rows = [r for r in rows if r["source_provenance"] == provenance]
    if tier:
        rows = [r for r in rows if r["strongest_tier"] == tier]
    if q:
        needle = q.lower()
        rows = [
            r
            for r in rows
            if needle in (r["company_name"] or "").lower() or needle in (r["title"] or "").lower()
        ]
    rows.sort(key=lambda r: (r["applied_at"] is None, r["applied_at"], r["id"]), reverse=True)
    page = rows[offset : offset + limit]
    return {
        "applications": page,
        "total": len(rows),
        "has_more": offset + len(page) < len(rows),
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


@router.get("/user/pipeline/{application_id}")
def pipeline_detail(application_id: int, user: AuthedUser = Depends(require_user)):
    """One application, its events, the messages behind them, and its actions.

    Events come oldest-first with `in_force` marked explicitly rather than left
    to position: latest-wins is per MESSAGE, so the row in force is not simply
    the last one in the list.
    """
    app = db.query_one(
        "SELECT id, job_id, company_name, title, applied_at, source_provenance, "
        "dismissed_at, dismissed_reason FROM applications WHERE id = %s AND user_id = %s",
        (application_id, user.id),
    )
    if app is None:
        raise HTTPException(status_code=404, detail="application not found")
    events = mail_pipeline.events_for(application_id)
    matches = db.query(
        """
            WITH touched AS (
                SELECT DISTINCT message_id FROM application_matches WHERE application_id = %(app)s
            ),
            current_match AS (
                SELECT DISTINCT ON (message_id) message_id, id
                FROM application_matches ORDER BY message_id, id DESC
            )
            SELECT am.id, am.message_id, am.application_id, am.method, am.confidence,
                   am.rationale, am.created_at, m.subject, m.from_email, m.sent_at,
                   (cm.id = am.id) AS in_force
            FROM application_matches am
            JOIN touched tm ON tm.message_id = am.message_id
            JOIN email_messages m ON m.id = am.message_id
            JOIN current_match cm ON cm.message_id = am.message_id
            ORDER BY am.message_id, am.id
            """,
        {"app": application_id},
    )
    # Every match carries what it was decided FROM, not just what it decided.
    # Without this a person has to open their mail client to check a match,
    # which is the same as not being able to check it.
    evidence = _evidence_for(sorted({m["message_id"] for m in matches}))
    return {
        **app,
        "stage": mail_pipeline.stage_for(events),
        "events": events,
        "matches": [{**m, "evidence": evidence.get(m["message_id"])} for m in matches],
        "actions": db.query(
            "SELECT * FROM action_items WHERE application_id = %s ORDER BY due_at NULLS LAST, id",
            (application_id,),
        ),
    }


DETACHED = "detached"


class Correction(BaseModel):
    note: str | None = None


def _owned_application(application_id: int, user_id: int) -> dict[str, Any]:
    app = db.query_one(
        "SELECT id, source_provenance, dismissed_at FROM applications "
        "WHERE id = %s AND user_id = %s",
        (application_id, user_id),
    )
    if app is None:
        raise HTTPException(status_code=404, detail="application not found")
    return app


@router.post("/user/pipeline/{application_id}/matches/{match_id}/detach")
def detach_match(
    application_id: int,
    match_id: int,
    body: Correction,
    user: AuthedUser = Depends(require_user),
):
    """This message does not belong to this application.

    Appends a match with a NULL application rather than deleting the old row.
    Latest-wins then takes the message out of the application, its events stop
    contributing, and the stage recomputes on its own - nobody restates it.
    The wrong match stays visible in the history, which is the point: a
    correction that erases its own cause cannot be reviewed.
    """
    _owned_application(application_id, user.id)
    match = db.query_one(
        "SELECT id, message_id FROM application_matches WHERE id = %s AND application_id = %s",
        (match_id, application_id),
    )
    if match is None:
        raise HTTPException(status_code=404, detail="match not found on this application")
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence, "
        "rationale) VALUES (%s, NULL, %s, 'none', %s)",
        (match["message_id"], DETACHED, body.note or "detached by the user"),
    )
    mail_pipeline.sync_action_items(application_id)
    return {"ok": True, "message_id": match["message_id"]}


@router.post("/user/pipeline/{application_id}/matches/{match_id}/reattach")
def reattach_match(
    application_id: int,
    match_id: int,
    body: Correction,
    user: AuthedUser = Depends(require_user),
):
    """Undo a detach, by the same append."""
    _owned_application(application_id, user.id)
    match = db.query_one("SELECT message_id FROM application_matches WHERE id = %s", (match_id,))
    if match is None:
        raise HTTPException(status_code=404, detail="match not found")
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence, "
        "rationale) VALUES (%s, %s, 'manual', 'high', %s)",
        (match["message_id"], application_id, body.note or "reattached by the user"),
    )
    mail_pipeline.sync_action_items(application_id)
    return {"ok": True, "message_id": match["message_id"]}


@router.post("/user/pipeline/{application_id}/dismiss")
def dismiss_application(
    application_id: int, body: Correction, user: AuthedUser = Depends(require_user)
):
    """This application should never have existed.

    Different from detaching a message: no correction to the matches fixes an
    application that mail invented, because removing one message leaves a
    shell that still has a stage.

    Refused on a tracker application. That row exists because the user entered
    it; mail evidence did not create it and no mail correction may remove it.
    Accepting the call would imply the mail pipeline owns something it does
    not.
    """
    app = _owned_application(application_id, user.id)
    if app["source_provenance"] != "email":
        raise HTTPException(
            status_code=409,
            detail="only a mail-derived application can be dismissed; this one came from the tracker",
        )
    db.execute(
        "UPDATE applications SET dismissed_at = now(), dismissed_reason = %s, updated_at = now() "
        "WHERE id = %s",
        (body.note, application_id),
    )
    return {"ok": True}


@router.post("/user/pipeline/{application_id}/restore")
def restore_application(
    application_id: int, body: Correction, user: AuthedUser = Depends(require_user)
):
    _owned_application(application_id, user.id)
    db.execute(
        "UPDATE applications SET dismissed_at = NULL, dismissed_reason = NULL, "
        "updated_at = now() WHERE id = %s",
        (application_id,),
    )
    return {"ok": True}


MANUAL = "manual"


class Assignment(BaseModel):
    """One of three targets. An application to attach to, a board job to
    create an application from, or a company and title when neither exists -
    which is the common case for mail predating the catalog."""

    application_id: int | None = None
    job_id: int | None = None
    company_name: str | None = None
    title: str | None = None
    note: str | None = None


def _owned_message(message_id: int, user_id: int) -> dict[str, Any]:
    row = db.query_one(
        "SELECT id, subject, from_email, sent_at, body_text FROM email_messages "
        "WHERE id = %s AND user_id = %s",
        (message_id, user_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="message not found")
    return row


@router.get("/user/messages/{message_id}/candidates")
def match_candidates(
    message_id: int,
    q: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    user: AuthedUser = Depends(require_user),
):
    """What this message could belong to, best guesses first.

    The default order is not a search ranking, it is the matcher's own
    reasoning made visible. `_by_company` REFUSES when two applications at one
    employer are both plausible, and those rejected candidates are exactly
    what a person should be shown first - the system already knows the answer
    is one of them and only declined to guess which.

    Board jobs with no application are included because the correction a user
    most often wants is "this belongs to a job I tracked but never recorded
    applying to", and there is nothing to attach to until one exists.
    """
    message = _owned_message(message_id, user.id)
    event = db.query_one(
        "SELECT detail FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    detail = (event or {}).get("detail") or {}
    company = detail.get("company")
    key = mail_match.norm_company(company)

    apps = db.query(
        "SELECT id, job_id, company_name, title, applied_at, source_provenance, dismissed_at "
        "FROM applications WHERE user_id = %s",
        (user.id,),
    )
    events = mail_pipeline.events_by_application(user.id)
    needle = (q or "").lower().strip()
    scored = []
    for app in apps:
        haystack = f"{app['company_name'] or ''} {app['title'] or ''}".lower()
        if needle and needle not in haystack:
            continue
        same_company = bool(key) and mail_match.norm_company(app["company_name"]) == key
        own = events.get(app["id"], [])
        scored.append(
            {
                **app,
                "stage": mail_pipeline.stage_for(own),
                "event_count": len(own),
                # Why it is on the list at all. A candidate the matcher
                # considered and declined to choose between is a different
                # thing from a search hit, and the UI should be able to say so.
                "reason": "same company as this mail" if same_company else "search match",
                "_rank": (0 if same_company else 1, app["company_name"] or ""),
            }
        )
    scored.sort(key=lambda r: r["_rank"])
    ambiguous = sum(1 for r in scored if r["reason"] == "same company as this mail")

    jobs = db.query(
        """
        SELECT j.id, j.company, j.title, j.url, uj.date_applied, uj.status
        FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
        WHERE uj.user_id = %(user)s
          AND NOT EXISTS (
              SELECT 1 FROM applications a WHERE a.user_id = uj.user_id AND a.job_id = uj.job_id
          )
          AND (%(q)s::text IS NULL OR lower(j.company) LIKE %(like)s OR lower(j.title) LIKE %(like)s)
        ORDER BY uj.date_applied DESC NULLS LAST
        LIMIT %(limit)s
        """,
        {"user": user.id, "q": q, "like": f"%{needle}%", "limit": limit},
    )
    return {
        "message": {
            "id": message["id"],
            "subject": message["subject"],
            "from_email": message["from_email"],
            "sent_at": message["sent_at"],
            "extracted_company": company,
            "extracted_title": detail.get("role_title"),
        },
        "applications": [
            {k: v for k, v in row.items() if not k.startswith("_")} for row in scored[:limit]
        ],
        "total_applications": len(scored),
        # The count the matcher choked on. Two or more means it refused on
        # purpose rather than finding nothing.
        "same_company_candidates": ambiguous,
        "board_jobs": jobs,
    }


@router.post("/user/messages/{message_id}/assign")
def assign_message(message_id: int, body: Assignment, user: AuthedUser = Depends(require_user)):
    """Attach this message to an application, creating one if asked.

    Appends. The previous match stays in the log and stops counting by
    latest-wins, so a correction never destroys the evidence for the decision
    it is correcting.

    A job_id creates the application from the tracked posting; a bare company
    and title creates a job-less one, which is the normal shape for mail
    predating the catalog. Never the other way round: an email does not get to
    invent a `jobs` row.
    """
    _owned_message(message_id, user.id)
    application_id = body.application_id

    if application_id is not None:
        owned = db.query_one(
            "SELECT id FROM applications WHERE id = %s AND user_id = %s",
            (application_id, user.id),
        )
        if owned is None:
            raise HTTPException(status_code=404, detail="application not found")
    elif body.job_id is not None:
        job = db.query_one(
            "SELECT j.id, j.company, j.title, uj.date_applied FROM jobs j "
            "JOIN user_jobs uj ON uj.job_id = j.id AND uj.user_id = %s WHERE j.id = %s",
            (user.id, body.job_id),
        )
        if job is None:
            raise HTTPException(status_code=404, detail="job not on your board")
        existing = db.query_one(
            "SELECT id FROM applications WHERE user_id = %s AND job_id = %s",
            (user.id, body.job_id),
        )
        if existing:
            application_id = existing["id"]
        else:
            created = db.query_one(
                "INSERT INTO applications (user_id, job_id, company_name, title, "
                "source_provenance, applied_at) VALUES (%s, %s, %s, %s, 'tracker', %s) "
                "RETURNING id",
                (user.id, job["id"], job["company"], job["title"], job["date_applied"]),
            )
            if created is None:
                raise HTTPException(status_code=500, detail="could not create the application")
            application_id = created["id"]
    elif body.company_name:
        created = db.query_one(
            "INSERT INTO applications (user_id, job_id, company_name, title, "
            "source_provenance, applied_at) VALUES (%s, NULL, %s, %s, 'manual', "
            "(SELECT sent_at FROM email_messages WHERE id = %s)) RETURNING id",
            (user.id, body.company_name, body.title, message_id),
        )
        if created is None:
            raise HTTPException(status_code=500, detail="could not create the application")
        application_id = created["id"]
    else:
        raise HTTPException(
            status_code=400,
            detail="give an application_id, a job_id, or a company_name to create one",
        )

    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence, "
        "rationale) VALUES (%s, %s, %s, 'high', %s)",
        (message_id, application_id, MANUAL, body.note or "assigned by the user"),
    )
    mail_pipeline.sync_action_items(application_id)
    return {"ok": True, "application_id": application_id}


# Enough to judge a match without opening the mail client, and short enough to
# send one per row. Two sentences either side of the company mention is what a
# person actually reads before deciding.
SNIPPET_RADIUS = 220


def _snippet(body: str | None, needle: str | None) -> dict[str, Any] | None:
    """The part of the message the match was made on, not the first 220 chars.

    An email's opening is a greeting and a logo alt-text; the sentence naming
    the company is somewhere in the middle. Centring the excerpt on that term
    is the difference between evidence and a preview - a preview shows that
    mail exists, evidence shows why it was attached to THIS application.
    """
    text = (body or "").strip()
    if not text:
        return None
    found = -1
    if needle:
        found = text.lower().find(needle.strip().lower())
    if found < 0:
        return {
            "text": text[: SNIPPET_RADIUS * 2],
            "centred_on": None,
            "truncated": len(text) > SNIPPET_RADIUS * 2,
        }
    start = max(0, found - SNIPPET_RADIUS)
    end = min(len(text), found + len(needle or "") + SNIPPET_RADIUS)
    return {
        "text": text[start:end],
        "centred_on": needle,
        "truncated": start > 0 or end < len(text),
    }


def _evidence_for(message_ids: list[int]) -> dict[int, dict[str, Any]]:
    """What each match actually rested on.

    The rationale field says what the matcher concluded; this says what it
    concluded it FROM. A person checking a match needs the second one - the
    company the classifier read out of the mail is the thing tier 2 and 3
    compared, so if that extraction is wrong the match is wrong and no amount
    of staring at the conclusion reveals it.
    """
    if not message_ids:
        return {}
    rows = db.query(
        """
        WITH current_event AS (
            SELECT DISTINCT ON (message_id) message_id, kind, confidence, detail, model
            FROM email_events ORDER BY message_id, id DESC
        )
        SELECT m.id, m.from_email, m.subject, m.sent_at, m.body_text,
               e.kind, e.confidence, e.detail, e.model
        FROM email_messages m
        LEFT JOIN current_event e ON e.message_id = m.id
        WHERE m.id = ANY(%s)
        """,
        (message_ids,),
    )
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        detail = row["detail"] or {}
        company = detail.get("company")
        out[row["id"]] = {
            # What the classifier read out of the mail. Tier 2 and 3 compare
            # THIS, not the raw text, so a wrong extraction is a wrong match.
            "extracted_company": company,
            "extracted_title": detail.get("role_title"),
            "classified_as": row["kind"],
            "classifier_confidence": row["confidence"],
            "classifier_model": row["model"],
            # The sender is the one fact no model produced. An ATS domain is
            # near-proof of a real application; a .edu sender usually is not.
            "from_domain": (row["from_email"] or "").split("@")[-1].lower() or None,
            "snippet": _snippet(row["body_text"], company),
            "body_chars": len(row["body_text"] or ""),
        }
    return out


@router.get("/user/messages/{message_id}")
def message_detail(message_id: int, user: AuthedUser = Depends(require_user)):
    """The whole message, for when the excerpt is not enough.

    Read-only and user-scoped. The body is already stored - withholding it
    would mean a person has to leave for their mail client to check a decision
    this system made, which is the same as not being able to check it.
    """
    row = db.query_one(
        "SELECT id, provider_message_id, provider_thread_id, source, from_email, from_name, "
        "to_emails, subject, sent_at, body_text, prefilter_hit, prefilter_reason "
        "FROM email_messages WHERE id = %s AND user_id = %s",
        (message_id, user.id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="message not found")
    return {
        **row,
        "events": db.query(
            "SELECT id, kind, confidence, occurred_at, deadline_at, deadline_inferred, detail, "
            "model, created_at FROM email_events WHERE message_id = %s ORDER BY id",
            (message_id,),
        ),
        "matches": db.query(
            """
            SELECT am.id, am.application_id, am.method, am.confidence, am.rationale,
                   am.created_at, a.company_name, a.title
            FROM application_matches am
            LEFT JOIN applications a ON a.id = am.application_id
            WHERE am.message_id = %s ORDER BY am.id
            """,
            (message_id,),
        ),
    }
