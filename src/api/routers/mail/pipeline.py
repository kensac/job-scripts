"""A person's applications, their derived stage, and the corrections to it.

Stage is derived from the mail rather than advanced by anything, so every
route here recomputes rather than reads a status column. A correction is an
append to the match log and the stage moves on its own.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import db
from api import params as params_
from api.auth import AuthedUser, require_user
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.routers.mail.shared import _evidence_for

router = APIRouter()


def _corrected_by(actor_user_id: int | None, viewer_id: int, model: str | None = None) -> str:
    """Who decided this row, from the viewer's point of view.

    FOUR ANSWERS, not three, and the fourth is the honest one. A machine wrote
    it; the viewer wrote it; an administrator wrote it; or a person wrote it
    before this column existed and there is no record of which person. That
    last case is not the same as "nobody corrected it" and must not be rendered
    as if it were - every human correction made before actor_user_id was added
    lands there, and the logs are append-only so it can never be resolved.

    The admin's corrections are surfaced to the affected user rather than
    hidden: a correction someone cannot see is one they cannot question, and
    finding your own data changed with no account of who changed it is worse
    than the change being visible.
    """
    if model is not None:
        return "model"
    if actor_user_id is None:
        return "unknown"
    return "you" if actor_user_id == viewer_id else "administrator"


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
        """
        SELECT a.id, a.job_id, a.company_name, a.title, a.applied_at,
               a.source_provenance, a.dismissed_at, a.dismissed_reason,
               uj.status AS board_status
        FROM applications a
        LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
        WHERE a.user_id = %s
        """,
        (user_id,),
    )
    events = mail_pipeline.events_by_application(user_id)
    tiers = _tiers_by_application(user_id)
    # Derived at read time and attached rather than stored, so it moves when
    # core.fetching.ats learns a provider or when a sender turns out to serve more
    # companies than it did when the match was made.
    senders = mail_pipeline.sender_signal(user_id)
    out = []
    for app in apps:
        own = events.get(app["id"], [])
        out.append(
            {
                **app,
                "stage": mail_pipeline.stage_for(own, app["board_status"]),
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


# A date that stands in for "no date" while sorting. It never leaves this
# module and never reaches a response - the boolean beside it is what actually
# orders undated rows, and this only stops the comparison raising on None.
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)

_STAGE_RANK = {
    name: index
    for index, name in enumerate(
        (
            "applied",
            "acknowledged",
            "assessment",
            "interviewing",
            "offer",
            "rejected",
            "closed",
            "withdrawn",
        )
    )
}

_PIPELINE_SORTS: dict[str, Any] = {
    "applied_at": lambda r: (r["applied_at"] is None, r["applied_at"] or _EPOCH, r["id"]),
    "last_event_at": lambda r: (r["last_event_at"] is None, r["last_event_at"] or _EPOCH, r["id"]),
    "company": lambda r: ((r["company_name"] or "").lower(), r["id"]),
    "title": lambda r: ((r["title"] or "").lower(), r["id"]),
    "stage": lambda r: (_STAGE_RANK.get(r["stage"], 99), r["id"]),
    "event_count": lambda r: (r["event_count"], r["id"]),
}


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
        # The stage vocabulary in process order with which stages are over,
        # so the lenses are read from here rather than copied.
        "stages": [
            {"key": key, "label": key.capitalize(), "terminal": key in mail_pipeline.TERMINAL}
            for key in _STAGE_RANK
        ],
    }


@router.get("/user/pipeline")
def pipeline(
    include_closed: bool = Query(default=False),
    stage: str | None = Query(default=None),
    provenance: str | None = Query(default=None),
    tier: str | None = Query(default=None),
    evidence: bool | None = Query(default=None),
    silent_days: int | None = Query(default=None, ge=0),
    sort: str = Query(default="applied_at"),
    dir: str = Query(default="desc"),
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
    # Comma-separated, because every lens worth having is multi-valued:
    # "waiting" is applied AND acknowledged, "over" is rejected AND closed AND
    # withdrawn. A single-valued filter makes a lens either impossible or a
    # client-side re-derivation of a set the server already knows.
    stages = {s.strip() for s in stage.split(",") if s.strip()} if stage else set()
    if stage == "dismissed":
        rows = [r for r in rows if r["dismissed_at"] is not None]
        stages = set()
    else:
        rows = [r for r in rows if r["dismissed_at"] is None]
    if not include_closed and not stages:
        rows = [r for r in rows if r["stage"] not in mail_pipeline.TERMINAL]
    if stages:
        rows = [r for r in rows if r["stage"] in stages]
    provenances, tiers = params_.csv(provenance), params_.csv(tier)
    if provenances:
        rows = [r for r in rows if r["source_provenance"] in provenances]
    if tiers:
        rows = [r for r in rows if r["strongest_tier"] in tiers]
    if silent_days is not None:
        # Applied, and nothing since. `evidence=false` is "no mail at all";
        # this is "no mail LATELY", which is the ghosting question and the one
        # a person actually asks. Measured against the last thing that
        # happened - a reply, or applying if nothing has - because silence
        # since an acknowledgement is still silence.
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=silent_days)
        rows = [
            r
            for r in rows
            if (r["last_event_at"] or r["applied_at"]) is not None
            and (r["last_event_at"] or r["applied_at"]) < cutoff
            and r["stage"] not in mail_pipeline.TERMINAL
        ]
    if evidence is not None:
        # The summary reports with_evidence and without_evidence; without this
        # the numbers are not clickable and the population behind them is
        # unreachable.
        rows = [r for r in rows if bool(r["event_count"]) is evidence]
    if q:
        needle = q.lower()
        rows = [
            r
            for r in rows
            if needle in (r["company_name"] or "").lower() or needle in (r["title"] or "").lower()
        ]
    # Sorted on the SET, not the page. A client sorting what it was handed
    # sorts one page of a filtered whole and reads as though it sorted
    # everything - which is a quiet lie rather than a missing feature.
    #
    # Stage sorts by how far through the process it is rather than
    # alphabetically, because "applied, acknowledged, interviewing" is the
    # order the word means and "acknowledged, applied, assessment" is not.
    key = _PIPELINE_SORTS.get(sort, _PIPELINE_SORTS["applied_at"])
    rows.sort(key=key, reverse=dir != "asc")
    page = rows[offset : offset + limit]
    return {
        "applications": page,
        "total": len(rows),
        "has_more": offset + len(page) < len(rows),
        "filters": params_.applied(provenance=provenances, tier=tiers),
        "actions": mail_pipeline.with_settling(
            db.query(
                """
            SELECT ai.*, a.company_name, a.title
            FROM action_items ai LEFT JOIN applications a ON a.id = ai.application_id
            WHERE ai.user_id = %s AND ai.resolved_at IS NULL
            ORDER BY ai.due_at NULLS LAST, ai.id
            """,
                (user.id,),
            )
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
        """
        SELECT a.id, a.job_id, a.company_name, a.title, a.applied_at,
               a.source_provenance, a.dismissed_at, a.dismissed_reason,
               uj.status AS board_status
        FROM applications a
        LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
        WHERE a.id = %s AND a.user_id = %s
        """,
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
                   am.rationale, am.created_at, am.actor_user_id,
                   m.subject, m.from_email, m.sent_at,
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
        "stage": mail_pipeline.stage_for(events, app["board_status"]),
        "events": events,
        "matches": [
            {
                **m,
                "evidence": evidence.get(m["message_id"]),
                # Derived, not stored: the same actor id reads as "you" to the
                # owner and as an administrator to anyone else, so there is no
                # second copy of that distinction to drift. NULL actor means
                # the matcher decided it.
                "corrected_by": _corrected_by(
                    m["actor_user_id"],
                    user.id,
                    # A match has no `model` column. The matcher's own rows
                    # carry its tier as the method; only a person writes
                    # 'manual' or 'detached'.
                    model=None if m["method"] in mail_match.HUMAN_METHODS else m["method"],
                ),
            }
            for m in matches
        ],
        # Both additions kept: #227 wraps actions in their settling state,
        # this branch adds who corrected each match. Independent answers to
        # the same question - what does this row let a person question.
        "actions": mail_pipeline.with_settling(
            db.query(
                "SELECT * FROM action_items WHERE application_id = %s "
                "ORDER BY due_at NULLS LAST, id",
                (application_id,),
            )
        ),
    }


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

    The same append as rejecting from the review queue, through the same
    function. Three endpoints wrote this table with a raw INSERT and none of
    them set `actor_user_id`, which is why 37 `manual` rows in production carry
    no actor and why "a person decided this" was unanswerable by query.
    """
    _owned_application(application_id, user.id)
    match = db.query_one(
        "SELECT id, message_id FROM application_matches WHERE id = %s AND application_id = %s",
        (match_id, application_id),
    )
    if match is None:
        raise HTTPException(status_code=404, detail="match not found on this application")
    mail_match.reject(
        match["message_id"], actor_user_id=user.id, note=body.note or "detached by the user"
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
    # Bound to the application, exactly as detach is. Owning the application
    # says nothing about owning the MATCH: without this the match_id was taken
    # from the request and trusted, so any match in the table - including
    # another user's - could have its message appended to an application the
    # caller does own, which is that message's content crossing to a stranger.
    match = db.query_one(
        "SELECT message_id FROM application_matches WHERE id = %s AND application_id = %s",
        (match_id, application_id),
    )
    if match is None:
        raise HTTPException(status_code=404, detail="match not found on this application")
    mail_match.record(
        match["message_id"],
        mail_match.Match(
            application_id, mail_match.MANUAL, "high", body.note or "reattached by the user"
        ),
        actor_user_id=user.id,
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
