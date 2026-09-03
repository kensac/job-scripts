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

import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import db, mail_match, mail_pipeline
from api.auth import AuthedUser, require_user
from api.routers.admin import require_admin
from api.tasks.mail_classify import EVENT_KINDS

router = APIRouter()


def _with_settling(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tags each action item with what could ever close it.

    An unresolved item means two different things and a caller cannot tell
    them apart from resolved_at alone: an assessment invite from last week is
    awaiting an event that may still arrive, while an offer from 2020 was
    never going to be closed by anything, because no email says "you
    accepted". An empty `settles_on` says the second, and such an item must
    not be rendered as a live obligation.
    """
    return [{**row, "settles_on": mail_pipeline.settles_on(row["kind"])} for row in rows]


_SORTABLE = {
    "sent_at": "m.sent_at",
    "imported_at": "m.imported_at",
    "id": "m.id",
}


# The wire name for "no match row at all", which is a third state and not an
# absence. `unmatched` means the matcher ran and found nothing;
# `not_an_application` means it correctly refused to look; NEVER_ATTEMPTED
# means nothing has run yet. The first two look identical in any aggregate and
# mean opposite things, and the third is the one worth filtering for when
# hunting failures - so it needs a name rather than being reachable only as a
# gap in a list.
NEVER_ATTEMPTED = "never_attempted"


def _where(
    *,
    kind: str | None,
    matched: bool | None,
    source: str | None,
    prefilter: bool | None,
    q: str | None,
    method: str | None = None,
    job_related: bool | None = None,
    classified: bool | None = None,
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
        #
        # But matched=false must NOT sweep in the deliberate refusals. A
        # recruiter approach belongs to no application by design, so counting
        # it as a matching failure turns correct behaviour into a defect on
        # screen - and it is 1,374 rows, which would swamp the real failures
        # in the pile a person goes to when hunting them.
        if matched:
            clauses.append("mt.application_id IS NOT NULL")
        else:
            clauses.append("mt.application_id IS NULL AND COALESCE(mt.method, '') <> %(refused)s")
            params["refused"] = mail_match.NOT_AN_APPLICATION
    if classified is not None:
        # THREE states, not two. "Not job related" and "nothing has looked at
        # it yet" are different facts and I collapsed them - the same mistake
        # as unmatched versus not_an_application, made in the filter written to
        # fix that one. e3 found it from the frontend: the prefilter matrix
        # inner-joins email_events and so counts only CLASSIFIED messages,
        # while job_related=false also matched the 16,182 unclassified, so the
        # cell linked to a larger set than the number it displayed.
        clauses.append("ev.kind IS NOT NULL" if classified else "ev.kind IS NULL")
    if job_related is not None:
        # `kind` is an equality filter, so "job-related" - the whole point of
        # the prefilter matrix - was not expressible: a person could open
        # "prefilter did not match" and not "job-related AND prefilter did not
        # match", which IS the 2,244 messages a gate would have dropped. That
        # is the number the gate decision rests on.
        #
        # NULL kind is not job-related here: nothing has classified it, so it
        # cannot be evidence either way.
        # Both branches now require a classification, so job_related is a
        # statement about what the classifier SAID rather than about whether it
        # has spoken. Ask `classified=false` for the backlog.
        clauses.append(
            "ev.kind IS NOT NULL AND ev.kind <> 'not_job_related'"
            if job_related
            else "ev.kind = 'not_job_related'"
        )
    if method == NEVER_ATTEMPTED:
        clauses.append("mt.match_id IS NULL")
    elif method:
        clauses.append("mt.method = %(method)s")
        params["method"] = method
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
    method: str | None = None,
    job_related: bool | None = None,
    classified: bool | None = None,
    q: str | None = None,
    sort: str = "sent_at",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
):
    where, params = _where(
        kind=kind,
        matched=matched,
        source=source,
        prefilter=prefilter,
        q=q,
        method=method,
        job_related=job_related,
        classified=classified,
    )
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


@router.get("/admin/mail/analytics")
def mail_analytics(
    days: int = Query(default=0, ge=0, le=3650),
    user: AuthedUser = Depends(require_admin),
):
    """Where the pipeline is wrong, as distributions rather than examples.

    /admin/mail answers "why did THIS message get that answer" and has to show
    the message to do it. That is the wrong tool for finding a pattern: reading
    a hundred messages to notice that one model is producing low-confidence
    answers is work a GROUP BY does in a second.

    days=0 means the whole corpus, which is the right default here. This is a
    historical import spanning 2018 to now, so a 30-day window would describe
    the tail of an eight-year backfill rather than the backfill.
    """
    window = "AND m.sent_at >= now() - make_interval(days => %(days)s)" if days else ""
    params: dict[str, Any] = {"days": days} if days else {}

    def q(sql: str) -> list[dict[str, Any]]:
        return db.query(sql.format(window=window), params)

    classification = q(
        """
        WITH ce AS (
            SELECT DISTINCT ON (message_id) message_id, kind, confidence, model, deadline_inferred,
                   detail
            FROM email_events ORDER BY message_id, id DESC
        )
        SELECT ce.kind, ce.model, ce.confidence,
               count(*) AS messages,
               count(*) FILTER (WHERE ce.detail->>'company' IS NULL) AS no_company,
               count(*) FILTER (WHERE ce.deadline_inferred) AS inferred_deadlines
        FROM email_messages m JOIN ce ON ce.message_id = m.id
        WHERE TRUE {window}
        GROUP BY 1, 2, 3 ORDER BY 4 DESC
        """
    )

    matching = q(
        """
        WITH ce AS (
            SELECT DISTINCT ON (message_id) message_id, kind
            FROM email_events ORDER BY message_id, id DESC
        ),
        cm AS (
            SELECT DISTINCT ON (message_id) message_id, application_id, method
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT ce.kind,
               coalesce(cm.method, 'never attempted') AS method,
               count(*) AS messages
        FROM email_messages m
        JOIN ce ON ce.message_id = m.id
        LEFT JOIN cm ON cm.message_id = m.id
        WHERE ce.kind <> 'not_job_related' {window}
        GROUP BY 1, 2 ORDER BY 3 DESC
        """
    )

    # The prefilter gates nothing on purpose - a filtered-out email is the one
    # unrecoverable failure, because the posting is closed and the thread is
    # not coming back. It survives to measure, after the fact, how much a gate
    # WOULD have missed. That is the only honest basis for ever letting the
    # ongoing feed use one, and it is a question only this endpoint can answer.
    prefilter = q(
        """
        WITH ce AS (
            SELECT DISTINCT ON (message_id) message_id, kind
            FROM email_events ORDER BY message_id, id DESC
        )
        SELECT coalesce(m.prefilter_hit, false) AS prefilter_hit,
               ce.kind <> 'not_job_related' AS job_related,
               count(*) AS messages
        FROM email_messages m JOIN ce ON ce.message_id = m.id
        WHERE TRUE {window}
        GROUP BY 1, 2
        """
    )
    missed = sum(r["messages"] for r in prefilter if not r["prefilter_hit"] and r["job_related"])
    job_related_total = sum(r["messages"] for r in prefilter if r["job_related"])

    senders = q(
        """
        WITH ce AS (
            SELECT DISTINCT ON (message_id) message_id, kind
            FROM email_events ORDER BY message_id, id DESC
        ),
        cm AS (
            SELECT DISTINCT ON (message_id) message_id, application_id
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT split_part(lower(m.from_email), '@', 2) AS domain,
               count(*) AS messages,
               count(*) FILTER (WHERE cm.application_id IS NOT NULL) AS matched
        FROM email_messages m
        JOIN ce ON ce.message_id = m.id
        LEFT JOIN cm ON cm.message_id = m.id
        WHERE ce.kind <> 'not_job_related' {window}
        GROUP BY 1 ORDER BY 2 DESC LIMIT 40
        """
    )

    # Deliberately NOT windowed, and named `corpus` so it cannot be read as
    # though it were. "How much mail exists, when does it start, how much is
    # still unclassified" are properties of the import, not of a slice: the
    # backlog question is about the whole eight-year backfill. by_source is
    # unwindowed for the same reason - it used to interpolate {window} while
    # the totals above it did not, so at days=30 the breakdown summed to less
    # than the total it was nested under and `oldest` reported 2018 on a
    # screen labelled "last 30 days".
    corpus = (
        db.query_one(
            """
        SELECT count(*) AS messages,
               count(*) FILTER (
                   WHERE NOT EXISTS (SELECT 1 FROM email_events e WHERE e.message_id = m.id)
               ) AS unclassified,
               min(m.sent_at) AS oldest,
               max(m.sent_at) AS newest
        FROM email_messages m
        """
        )
        or {}
    )
    domain_total = db.query_one(
        "SELECT count(DISTINCT split_part(lower(m.from_email), '@', 2)) AS domains "
        "FROM email_messages m JOIN ("
        "  SELECT DISTINCT ON (message_id) message_id, kind FROM email_events"
        "  ORDER BY message_id, id DESC) ce ON ce.message_id = m.id "
        f"WHERE ce.kind <> 'not_job_related' {window}",
        params,
    )

    return {
        "window_days": days or None,
        "corpus": {
            **corpus,
            "by_source": db.query(
                "SELECT m.source, count(*) AS messages FROM email_messages m "
                "GROUP BY 1 ORDER BY 2 DESC"
            ),
        },
        # Each section below counts a DIFFERENT population, and nothing in the
        # rows says so. Presented side by side they invite a subtraction that
        # means nothing, so the denominators ship here rather than being
        # hardcoded by whoever renders them.
        "populations": {
            "classification": {
                "messages": sum(r["messages"] for r in classification),
                "excludes": [],
            },
            "matching": {
                "messages": sum(r["messages"] for r in matching),
                "excludes": ["not_job_related"],
            },
            "prefilter": {
                "messages": sum(r["messages"] for r in prefilter),
                "excludes": [],
            },
            "sender_domains": {
                "messages": sum(r["messages"] for r in senders),
                "excludes": ["not_job_related"],
                "domains_shown": len(senders),
                "domains_total": (domain_total or {}).get("domains", 0),
            },
        },
        "classification": classification,
        "matching": matching,
        # A rate the prefilter could never report about itself, and the reason
        # it was kept as a signal rather than deleted.
        "prefilter": {
            "cells": prefilter,
            "job_related_a_gate_would_have_dropped": missed,
            "job_related_total": job_related_total,
        },
        "sender_domains": senders,
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
    # WHICH no-match this is, when application_id is null. Without it every
    # admin no-match was recorded as method='manual' with a null application,
    # which the unmatched predicate reads as a matcher FAILURE - so an admin
    # who correctly decided a recruiter approach belongs to no application put
    # it straight back into the queue of things needing attention, and nothing
    # said so. `not_an_application` is not `unmatched`: deliberately attached
    # to nothing versus looked and found nothing, a distinction that has now
    # mattered on six surfaces.
    #
    # Default keeps the old behaviour for callers that do not send it: a
    # no-match with no reason given is a failure to find one, which is the
    # weaker claim and the safe one.
    outcome: Literal["no_application_found", "not_an_application"] = "no_application_found"


def _resync_applications(message_id: int) -> list[int]:
    """Resync every application this message feeds, and say which they were.

    A changed kind can change what an application is waiting for, and it can
    change an application the person is not currently looking at - so the ids
    come back rather than an `ok`. Both the correction and the revert need
    this, which is why it is one function.
    """
    affected = [
        r["application_id"]
        for r in db.query(
            """
            SELECT DISTINCT application_id FROM application_matches
            WHERE message_id = %s AND application_id IS NOT NULL
            """,
            (message_id,),
        )
    ]
    for application_id in affected:
        mail_pipeline.sync_action_items(application_id)
    return affected


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


def _admin_message(message_id: int) -> dict[str, Any]:
    """Any user's message, for an administrator.

    Deliberately NOT `_owned_message`: the whole point of these routes is that
    the admin corrects other people's mailboxes - friends and family, not a
    hypothetical. The ownership rule that applies is `require_admin` on the
    route; what this returns is the OWNER, because every helper below needs to
    be scoped to them rather than to the caller.
    """
    message = db.query_one(
        "SELECT id, user_id, subject, from_email, sent_at, body_text "
        "FROM email_messages WHERE id = %s",
        (message_id,),
    )
    if message is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown message"})
    return message


@router.get("/admin/mail/{message_id}/candidates")
def admin_match_candidates(
    message_id: int,
    q: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    user: AuthedUser = Depends(require_admin),
):
    """The same picker the user gets, over the message owner's applications.

    The admin panel offered a bare application-id field, which requires
    knowing an id that is not displayed anywhere - so the only correction
    available in practice was no correction. This is the identical ranking the
    user side computes, including the candidates `_by_company` refused to
    choose between, which are the ones a person is best placed to settle.
    """
    message = _admin_message(message_id)
    return _candidates_payload(message, message["user_id"], q, limit)


@router.post("/admin/mail/{message_id}/classify")
def admin_correct_classification(
    message_id: int, body: Reclassification, user: AuthedUser = Depends(require_admin)
):
    """Correct what a message IS, in someone else's mailbox.

    Recorded against the admin rather than the owner: `actor_user_id` is the
    caller, so a later reader can tell an administrator's correction from the
    owner's own without a second flag saying which.
    """
    message = _admin_message(message_id)
    return _apply_classification(message, body, actor_user_id=user.id)


@router.post("/admin/mail/{message_id}/classify/revert")
def admin_revert_classification(message_id: int, user: AuthedUser = Depends(require_admin)):
    """Undo a correction by restoring the model's last answer."""
    _admin_message(message_id)
    return _apply_revert(message_id, actor_user_id=user.id)


@router.post("/admin/mail/{message_id}/match")
def override_match(message_id: int, body: MatchOverride, user: AuthedUser = Depends(require_admin)):
    """Correct a match by hand.

    An append, not an edit: the matcher's own attempt survives underneath, so
    a systematically wrong tier stays visible in the history instead of being
    quietly papered over one row at a time. That history is the only evidence
    that the matcher needs fixing rather than the row.

    A null application_id needs `outcome` to say WHICH no-match is meant,
    because the two are not the same fact and the queue treats them
    differently. Attaching to an application ignores it - the outcome of a
    match is the match.
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
    # A deliberate refusal is recorded as the matcher's own refusal method, so
    # every reader that already distinguishes the two - the unmatched cut, the
    # analytics breakdown, the pipeline filter - sees it without being taught a
    # fourth value. Agreeing with the matcher should look like agreeing with it.
    if body.application_id is None and body.outcome == "not_an_application":
        method, confidence = mail_match.NOT_AN_APPLICATION, "high"
    else:
        method = MANUAL
        confidence = "high" if body.application_id is not None else "none"
    mail_match.record(
        message_id,
        actor_user_id=user.id,
        match=mail_match.Match(body.application_id, method, confidence, f"set by admin {user.sub}"),
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
    # core.ats learns a provider or when a sender turns out to serve more
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
    if provenance:
        rows = [r for r in rows if r["source_provenance"] == provenance]
    if tier:
        rows = [r for r in rows if r["strongest_tier"] == tier]
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
        "actions": _with_settling(
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
                    model=None if m["method"] in _HUMAN_MATCH_METHODS else m["method"],
                ),
            }
            for m in matches
        ],
        # Both additions kept: #227 wraps actions in their settling state,
        # this branch adds who corrected each match. Independent answers to
        # the same question - what does this row let a person question.
        "actions": _with_settling(
            db.query(
                "SELECT * FROM action_items WHERE application_id = %s "
                "ORDER BY due_at NULLS LAST, id",
                (application_id,),
            )
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

# Methods only a person writes. Every other method is one of the matcher's
# tiers, so the method column is what tells a human match from a machine one -
# `application_matches` has no `model` column to ask instead.
_HUMAN_MATCH_METHODS = frozenset({MANUAL, DETACHED})


# How many messages one assignment may carry. A conversation is a handful of
# messages; a number far above that means the grouping is wrong rather than the
# thread being long, and silently reassigning hundreds of messages on one click
# is the failure worth refusing.
MAX_THREAD_FANOUT = 40

# A conversation's key, derived rather than stored.
#
# `provider_thread_id` is the FIRST References entry, which is the Message-ID
# of the message that started the thread. So replies carry it and the ROOT does
# not - a first message has no References, by definition. That left 1,113
# messages as the origin of a thread that exists in the database without being
# part of it: assigning a reply moved its siblings and silently left the
# original behind.
#
# Coalescing to the message's own id closes it, because a root's Message-ID IS
# the key its replies carry. A message with no thread and no replies coalesces
# to its own id and matches nothing else, so this cannot group unrelated mail -
# which is the failure that made subject-based grouping unsafe.
_THREAD_KEY = "COALESCE(m.provider_thread_id, m.provider_message_id)"


class Assignment(BaseModel):
    """One of three targets. An application to attach to, a board job to
    create an application from, or a company and title when neither exists -
    which is the common case for mail predating the catalog."""

    application_id: int | None = None
    job_id: int | None = None
    company_name: str | None = None
    title: str | None = None
    note: str | None = None
    # Whether to carry the whole conversation. Default on, because a person
    # correcting one message of a thread means the thread, and making them do
    # it five times is the kind of chore this system exists to remove.
    whole_thread: bool = True


def _owned_message(message_id: int, user_id: int) -> dict[str, Any]:
    row = db.query_one(
        "SELECT id, subject, from_email, sent_at, body_text FROM email_messages "
        "WHERE id = %s AND user_id = %s",
        (message_id, user_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="message not found")
    return row


_USER_MAIL_SORTS = {
    "sent_at": "m.sent_at",
    "from_email": "lower(m.from_email)",
    "subject": "lower(m.subject)",
    "kind": "ce.kind",
    "company": "lower(coalesce(a.company_name, ce.detail->>'company'))",
}


@router.get("/user/mail")
def user_mail(
    kind: str | None = Query(default=None),
    matched: bool | None = Query(default=None),
    classified: bool | None = Query(default=None),
    application_id: int | None = Query(default=None),
    sort: str = Query(default="sent_at"),
    dir: str = Query(default="desc"),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
):
    """The user's own mail, and what the pipeline did with each message.

    There was no way to see this without being an admin. `/admin/mail` is the
    debug view - it exists to answer "why did the classifier decide that", it
    spans every user, and it is gated behind infra-admin. Reading your own
    inbox should not require the permission to read everyone's.

    The two surfaces answer different questions and so are not the same query.
    An admin asks which messages the pipeline handled badly; a person asks what
    arrived and where it went. This returns the second: the current
    classification, whether it reached an application, and which one.
    """
    where = ["m.user_id = %(user)s"]
    params: dict[str, Any] = {"user": user.id, "limit": limit, "offset": offset}
    if kind:
        # Comma-separated for the same reason as stage: "replies" is rejection
        # AND offer AND interview_invite AND assessment_invite, and a lens the
        # client assembles from four requests is not the same set.
        kinds = [k.strip() for k in kind.split(",") if k.strip()]
        where.append("ce.kind = ANY(%(kinds)s)")
        params["kinds"] = kinds
    if q:
        where.append("(m.subject ILIKE %(q)s OR m.from_email ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if classified is not None:
        # The backlog, reachable by its own name. Excluded from the unmatched
        # queue because nothing has looked at it yet, which is a different
        # state from the matcher having failed - and a queue that mixes them
        # asks a person to fix something no decision has been made about.
        where.append("ce.kind IS NOT NULL" if classified else "ce.kind IS NULL")
    if application_id is not None:
        # The reverse trip. Without it an application can only link to a
        # company text search, which returns a DIFFERENT set and would quietly
        # lie about being "the messages behind this application".
        where.append("cm.application_id = %(app)s")
        params["app"] = application_id
    if matched is True:
        where.append("cm.application_id IS NOT NULL")
    elif matched is False:
        # A queue of things to fix, not a list of everything without an
        # application. Personal mail correctly has no application and always
        # will, so including it made "unmatched" 63,598 - essentially the whole
        # mailbox - when the population a person can actually act on is 4,458.
        #
        # Three exclusions, each for its own reason:
        #   not_job_related      already right, and 83% of the corpus
        #   unclassified         nothing has looked yet; that is a backlog,
        #                        reachable as classified=false
        #   not_an_application   the matcher refused ON PURPOSE - a recruiter
        #                        approach belongs to no application by design
        #
        # I fixed the third on /admin/mail in #237 and left this endpoint
        # alone, which is how the queue built on it shipped useless.
        where.append(
            "cm.application_id IS NULL "
            "AND ce.kind IS NOT NULL AND ce.kind <> 'not_job_related' "
            "AND COALESCE(cm.method, '') <> %(refused)s"
        )
        params["refused"] = mail_match.NOT_AN_APPLICATION
    predicate = " AND ".join(where)

    base = f"""
        FROM email_messages m
        LEFT JOIN (
            SELECT DISTINCT ON (message_id) message_id, kind, confidence, detail
            FROM email_events ORDER BY message_id, id DESC
        ) ce ON ce.message_id = m.id
        LEFT JOIN (
            SELECT DISTINCT ON (message_id) message_id, application_id, method
            FROM application_matches ORDER BY message_id, id DESC
        ) cm ON cm.message_id = m.id
        LEFT JOIN applications a ON a.id = cm.application_id
        WHERE {predicate}
    """
    total = db.query_one(f"SELECT count(*) AS n {base}", params)
    # A whitelist, not interpolation of whatever arrives: this string is
    # concatenated into SQL, and an unknown value falls back rather than
    # reaching the database.
    order = _USER_MAIL_SORTS.get(sort, _USER_MAIL_SORTS["sent_at"])
    direction = "ASC" if dir == "asc" else "DESC"
    rows = db.query(
        f"""
        SELECT m.id, m.subject, m.from_email, m.sent_at, m.source,
               ce.kind, ce.confidence,
               ce.detail->>'company' AS extracted_company,
               cm.application_id, cm.method,
               a.company_name, a.title
        {base}
        ORDER BY {order} {direction} NULLS LAST, m.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    # Per-kind counts over the SAME predicate, so a tab's number and its
    # contents cannot disagree. One extra aggregate rather than one request per
    # tab - and without it mail tabs either show no counts or cost a round trip
    # each to display a number the server already had in hand.
    by_kind = {
        r["kind"]: r["n"]
        for r in db.query(f"SELECT ce.kind, count(*) AS n {base} GROUP BY ce.kind", params)
        if r["kind"]
    }
    return {
        "messages": rows,
        "total": (total or {}).get("n", 0),
        "has_more": offset + len(rows) < (total or {}).get("n", 0),
        "by_kind": by_kind,
    }


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
    return _candidates_payload(message, user.id, q, limit)


def _candidates_payload(
    message: dict[str, Any], owner_id: int, q: str | None, limit: int
) -> dict[str, Any]:
    """Candidates for a message, scoped to the message's OWNER.

    Separated from the route because the admin view asks the same question
    about somebody else's mailbox. The owner is a parameter rather than the
    caller precisely so that difference is stated once instead of a second
    copy of the ranking drifting from this one.
    """
    message_id = message["id"]
    event = db.query_one(
        "SELECT detail FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    detail = (event or {}).get("detail") or {}
    company = detail.get("company")
    key = mail_match.norm_company(company)

    apps = db.query(
        """
        SELECT a.id, a.job_id, a.company_name, a.title, a.applied_at,
               a.source_provenance, a.dismissed_at, uj.status AS board_status
        FROM applications a
        LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
        WHERE a.user_id = %s
        """,
        (owner_id,),
    )
    events = mail_pipeline.events_by_application(owner_id)
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
                "stage": mail_pipeline.stage_for(own, app["board_status"]),
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
        {"user": owner_id, "q": q, "like": f"%{needle}%", "limit": limit},
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

    # The provider's own thread id, never a derived one. Grouping threadless
    # mail by normalised subject and sender was measured and is unsafe: "thank
    # you for applying!" from myworkday.com is 49 messages from 49 DIFFERENT
    # employers, and merging those would attach 49 unrelated applications to
    # one. The correct signal is the References/In-Reply-To chain, which this
    # importer discards - `headers` is empty on all 67k rows - so until that is
    # fixed, threadless mail is assigned one message at a time.
    targets = [message_id]
    if body.whole_thread:
        siblings = db.query(
            f"""
            SELECT m.id FROM email_messages m
            WHERE m.user_id = %(user)s
              AND {_THREAD_KEY} = (
                  SELECT {_THREAD_KEY} FROM email_messages m WHERE m.id = %(msg)s
              )
              AND m.id <> %(msg)s
            ORDER BY m.id
            LIMIT %(cap)s
            """,
            {"user": user.id, "msg": message_id, "cap": MAX_THREAD_FANOUT},
        )
        targets.extend(r["id"] for r in siblings)

    rationale = body.note or "assigned by the user"
    for index, target in enumerate(targets):
        db.execute(
            "INSERT INTO application_matches (message_id, application_id, method, confidence, "
            "rationale) VALUES (%s, %s, %s, 'high', %s)",
            (
                target,
                application_id,
                MANUAL,
                rationale if index == 0 else f"{rationale} (same conversation)",
            ),
        )
    mail_pipeline.sync_action_items(application_id)
    return {
        "ok": True,
        "application_id": application_id,
        # Say how many moved. One click that quietly reassigns a dozen messages
        # should report it rather than have the count discovered later.
        "messages_assigned": len(targets),
    }


def _mention(body: str | None, needle: str | None) -> dict[str, Any] | None:
    """The whole message, and WHERE the company is mentioned in it.

    Not an excerpt. An excerpt meant reading a fragment, clicking, and then
    reading the same message again from the top with no marker on the part that
    mattered - the reader did the finding twice and we helped neither time.

    Offsets rather than a highlighted string, so the client decides how to mark
    it and the body stays exactly what the sender wrote. `start` is null when
    the company does not appear verbatim, which is common: the classifier reads
    it off a signature or a logo as often as out of a sentence.

    The body is already capped at MAX_BODY_CHARS on the way in, so this cannot
    be unbounded no matter how long the original was.
    """
    text = (body or "").strip()
    if not text:
        return None
    term = (needle or "").strip()
    found = text.lower().find(term.lower()) if term else -1
    return {
        "text": text,
        "start": found if found >= 0 else None,
        "end": found + len(term) if found >= 0 else None,
        "term": term if found >= 0 else None,
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
            "mention": _mention(row["body_text"], company),
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


# What the mail implies the board should say. Only kinds where the evidence is
# unambiguous about the OUTCOME - an acknowledgement means the application is
# alive, which the board already says, so it suggests nothing.
_STATUS_FROM_EVENT = {
    "rejection": "Rejected",
    "position_closed": "No Longer Available",
    "offer": "Offer",
    "interview_invite": "Interviewing",
    "interview_scheduled": "Interviewing",
}

# Board statuses that mean "still waiting". A suggestion is only worth making
# against one of these: if he has already moved it on, the mail is confirming
# what he knows rather than telling him something.
_UNRESOLVED_STATUSES = ("Application Submitted", "Follow-up")

ACCEPTED = "accepted"
DISMISSED = "dismissed"


class SuggestionAnswer(BaseModel):
    response: str
    note: str | None = None


@router.get("/user/suggestions")
def suggestions(user: AuthedUser = Depends(require_user)):
    """Where the mail and the board disagree, as things to confirm.

    Never an overwrite. `user_jobs.status` is what the user typed, and a system
    that silently rewrites it stops being trustworthy at exactly the moment it
    is most confident. So this says "we think you were rejected" and waits.

    Derived at read time rather than stored, so a suggestion disappears on its
    own once he acts on the board directly, or once a reclassification retracts
    the evidence. Only his ANSWER is a fact worth keeping.

    Every row carries the evidence - the message, the sender, and an excerpt -
    because a suggestion he cannot check is one he has to take on faith.
    """
    rows = db.query(
        """
        WITH current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id
            FROM application_matches ORDER BY message_id, id DESC
        ),
        current_event AS (
            SELECT DISTINCT ON (message_id) message_id, id, kind, detail
            FROM email_events ORDER BY message_id, id DESC
        )
        SELECT DISTINCT ON (a.id, e.kind)
               a.id AS application_id, a.company_name, a.title, a.job_id,
               uj.status AS board_status, uj.date_applied,
               e.id AS event_id, e.kind, m.id AS message_id, m.sent_at
        FROM applications a
        JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
        JOIN current_match cm ON cm.application_id = a.id
        JOIN current_event e ON e.message_id = cm.message_id
        JOIN email_messages m ON m.id = cm.message_id
        WHERE a.user_id = %(user)s
          AND a.dismissed_at IS NULL
          AND uj.status = ANY(%(unresolved)s)
          AND e.kind = ANY(%(kinds)s)
          AND NOT EXISTS (
              SELECT 1 FROM suggestion_responses sr
              WHERE sr.application_id = a.id AND sr.event_id = e.id
          )
        ORDER BY a.id, e.kind, e.id DESC
        """,
        {
            "user": user.id,
            "unresolved": list(_UNRESOLVED_STATUSES),
            "kinds": sorted(_STATUS_FROM_EVENT),
        },
    )
    evidence = _evidence_for(sorted({r["message_id"] for r in rows}))
    return {
        "suggestions": [
            {
                **row,
                "suggested_status": _STATUS_FROM_EVENT[row["kind"]],
                "evidence": evidence.get(row["message_id"]),
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.post("/user/suggestions/{application_id}/{event_id}")
def answer_suggestion(
    application_id: int,
    event_id: int,
    body: SuggestionAnswer,
    user: AuthedUser = Depends(require_user),
):
    """Accept a suggestion and the board moves; dismiss it and it stays put.

    Both are recorded against the EVENT, so a dismissal silences this piece of
    evidence rather than the question. A later rejection from the same company
    is new evidence and gets asked again - which is what makes dismissing safe
    rather than a decision he can never revisit.
    """
    if body.response not in (ACCEPTED, DISMISSED):
        raise HTTPException(status_code=400, detail=f"response must be {ACCEPTED} or {DISMISSED}")
    app = db.query_one(
        "SELECT id, job_id FROM applications WHERE id = %s AND user_id = %s",
        (application_id, user.id),
    )
    if app is None:
        raise HTTPException(status_code=404, detail="application not found")
    # The event has to belong to a message currently matched to THIS
    # application - the same join the suggestion list is built from. Checking
    # only that the application is owned left event_id taken from the request
    # and trusted, so another user's event could decide which status this
    # application moved to.
    event = db.query_one(
        """
        WITH current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT e.id, e.kind
        FROM email_events e
        JOIN current_match cm ON cm.message_id = e.message_id
        WHERE e.id = %s AND cm.application_id = %s
        """,
        (event_id, application_id),
    )
    if event is None or event["kind"] not in _STATUS_FROM_EVENT:
        raise HTTPException(status_code=404, detail="no suggestion for that event")

    status = _STATUS_FROM_EVENT[event["kind"]]
    db.execute(
        "INSERT INTO suggestion_responses (user_id, application_id, event_id, "
        "suggested_status, response) VALUES (%s, %s, %s, %s, %s)",
        (user.id, application_id, event_id, status, body.response),
    )
    if body.response == ACCEPTED and app["job_id"] is not None:
        db.execute(
            "UPDATE user_jobs SET status = %s, updated_at = now() "
            "WHERE user_id = %s AND job_id = %s",
            (status, user.id, app["job_id"]),
        )
    return {"ok": True, "status": status if body.response == ACCEPTED else None}


class ActionAnswer(BaseModel):
    note: str | None = None


@router.post("/user/actions/{action_id}/resolve")
def resolve_action(action_id: int, body: ActionAnswer, user: AuthedUser = Depends(require_user)):
    """Mark an action done, because for some kinds nothing else ever will.

    Auto-resolution carries most of the weight and should: an assessment invite
    is closed by the acknowledgement that follows it, not by the user
    remembering. That is what makes this no-touch rather than a second inbox.

    But two kinds have no settling event at all. `respond_to_offer` closes only
    on a rejection, so accepting an offer, declining it or signing never
    settles it - 146 open and none has ever closed. `reply_to_recruiter` has an
    empty settling set by construction. For those, a person is the only
    producer, exactly as the board is the only producer of `withdrawn`.

    Guarded on the event id in sync_action_items, so a resolved item is not
    reopened by the next recomputation.
    """
    row = db.query_one(
        "SELECT id, resolved_at FROM action_items WHERE id = %s AND user_id = %s",
        (action_id, user.id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    if row["resolved_at"] is not None:
        return {"ok": True, "already": True}
    db.execute(
        "UPDATE action_items SET resolved_at = now(), resolution = %s WHERE id = %s",
        (body.note or "marked done", action_id),
    )
    return {"ok": True, "already": False}


@router.post("/user/actions/{action_id}/reopen")
def reopen_action(action_id: int, body: ActionAnswer, user: AuthedUser = Depends(require_user)):
    """Undo a manual resolution.

    Refused on one that a later event settled: that is a fact about the mail
    rather than a decision the user made, and reopening it would only have it
    close again on the next recomputation.
    """
    row = db.query_one(
        "SELECT id, resolved_by_event_id FROM action_items WHERE id = %s AND user_id = %s",
        (action_id, user.id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    if row["resolved_by_event_id"] is not None:
        raise HTTPException(
            status_code=409,
            detail="a later email settled this; it would close again on the next pass",
        )
    db.execute(
        "UPDATE action_items SET resolved_at = NULL, resolution = NULL WHERE id = %s",
        (action_id,),
    )
    return {"ok": True}


class Reclassification(BaseModel):
    kind: str
    company: str | None = None
    role_title: str | None = None
    note: str | None = None


@router.post("/user/messages/{message_id}/classify")
def correct_classification(
    message_id: int, body: Reclassification, user: AuthedUser = Depends(require_user)
):
    """Say what a message actually is, when the classifier got it wrong.

    Every other correction here fixes the MATCH. Nothing fixed the kind - and
    stage is derived from kinds, so a rejection read as an acknowledgement
    silently moves an application and the only affordance on offer was
    detaching a match that was correct.

    Appends an event rather than editing one. Events are append-only and the
    latest per message wins, so this supersedes the model's answer by the same
    rule a re-classification does, and the wrong answer stays visible in the
    history. model is NULL, which is how a human correction is told apart from
    a model's: nothing else writes an event without one.

    The stage recomputes on read, so the correction propagates with nobody
    restating it - the same property that makes detaching work.
    """
    message = _owned_message(message_id, user.id)
    return _apply_classification(message, body, actor_user_id=user.id)


def _apply_classification(
    message: dict[str, Any], body: Reclassification, *, actor_user_id: int
) -> dict[str, Any]:
    """Append a corrected classification, recording WHO corrected it.

    `actor_user_id` is not always the message's owner: an administrator
    correcting somebody else's mailbox writes their own id here, and whether
    that was a self-correction is derived by comparing the two rather than
    stored a second time.
    """
    message_id = message["id"]
    if body.kind not in EVENT_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {sorted(EVENT_KINDS)}")
    current = db.query_one(
        "SELECT detail FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    # Carry the extraction forward unless the correction replaces it. A person
    # fixing "acknowledgement" to "rejection" is not also asserting the company
    # was wrong, and blanking it would break the matching that already worked.
    detail = dict((current or {}).get("detail") or {})
    if body.company is not None:
        detail["company"] = body.company
    if body.role_title is not None:
        detail["role_title"] = body.role_title
    if body.note:
        detail["corrected_note"] = body.note
    detail["corrected_by_user"] = True

    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model, actor_user_id) "
        "VALUES (%s, %s, 'high', %s, NULL, %s)",
        (message_id, body.kind, db.jsonb(detail), actor_user_id),
    )
    affected = _resync_applications(message_id)
    return {
        "ok": True,
        "message_id": message["id"],
        "kind": body.kind,
        "affected_application_ids": affected,
    }


@router.get("/user/message-kinds")
def message_kinds(user: AuthedUser = Depends(require_user)):
    """The vocabulary, served rather than copied.

    The client kept this list in two places and it drifts the moment a kind is
    added - the same failure as the stage vocabulary, which had a terminal
    state the frontend did not know about.
    """
    return {"kinds": sorted(EVENT_KINDS)}


@router.post("/user/messages/{message_id}/classify/revert")
def revert_classification(message_id: int, user: AuthedUser = Depends(require_user)):
    """Undo a correction by restoring what the model last said.

    Another append, not a delete: a mis-correction has to be recoverable and
    the log still has to show that both happened. Refused when the model has
    never classified this message, because there is nothing to restore.
    """
    _owned_message(message_id, user.id)
    return _apply_revert(message_id, actor_user_id=user.id)


def _apply_revert(message_id: int, *, actor_user_id: int) -> dict[str, Any]:
    """Restore the model's last answer, recording who asked for the restore."""
    model_answer = db.query_one(
        "SELECT kind, confidence, detail, model FROM email_events "
        "WHERE message_id = %s AND model IS NOT NULL ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    if model_answer is None:
        raise HTTPException(status_code=409, detail="no model classification to restore")
    current = db.query_one(
        "SELECT model FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    if current and current["model"] is not None:
        return {"ok": True, "already": True, "kind": model_answer["kind"]}

    detail = dict(model_answer["detail"] or {})
    detail.pop("corrected_by_user", None)
    detail.pop("corrected_note", None)
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model, actor_user_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            message_id,
            model_answer["kind"],
            model_answer["confidence"],
            db.jsonb(detail),
            model_answer["model"],
            actor_user_id,
        ),
    )
    # The same affected ids classify returns. A revert moves the derived stage
    # exactly as a correction does, and possibly on an application nobody is
    # looking at, so it cannot be an {ok: true}.
    return {
        "ok": True,
        "already": False,
        "kind": model_answer["kind"],
        "affected_application_ids": _resync_applications(message_id),
    }


@router.get("/user/messages/{message_id}/thread")
def read_thread(
    message_id: int,
    limit: int = Query(default=MAX_THREAD_FANOUT, ge=1, le=200),
    user: AuthedUser = Depends(require_user),
):
    """The conversation this message belongs to, oldest first.

    Mail is a flat list of messages and the unit a person thinks in is the
    exchange: 19,995 messages sit in 4,141 conversations of more than one, so
    roughly a third of the corpus is currently shown out of its context.

    Keyed on COALESCE(thread id, own message id), the same derivation #235
    introduced - the provider's thread id is the first References entry, which
    every reply carries and the message that STARTED the thread does not. Left
    alone it excludes the original from its own conversation.

    Capped and honest about it. The longest key in this corpus holds 474
    messages, which is a mailing list reusing a thread id rather than a
    conversation, and a reader who asked for a thread should not be handed one.
    """
    _owned_message(message_id, user.id)
    rows = db.query(
        f"""
        WITH key AS (
            SELECT {_THREAD_KEY} AS k FROM email_messages m WHERE m.id = %(msg)s
        ),
        current_event AS (
            SELECT DISTINCT ON (message_id) message_id, kind, confidence, detail
            FROM email_events ORDER BY message_id, id DESC
        ),
        current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id, method
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT m.id, m.subject, m.from_email, m.from_name, m.sent_at, m.source,
               m.body_text, ce.kind, ce.confidence,
               ce.detail->>'company' AS extracted_company,
               cm.application_id, cm.method,
               a.company_name, a.title
        FROM email_messages m
        LEFT JOIN current_event ce ON ce.message_id = m.id
        LEFT JOIN current_match cm ON cm.message_id = m.id
        LEFT JOIN applications a ON a.id = cm.application_id
        WHERE m.user_id = %(user)s AND {_THREAD_KEY} = (SELECT k FROM key)
        ORDER BY m.sent_at, m.id
        LIMIT %(limit)s
        """,
        {"user": user.id, "msg": message_id, "limit": limit + 1},
    )
    truncated = len(rows) > limit
    return {
        "messages": rows[:limit],
        "total": len(rows[:limit]),
        # Said rather than implied. A conversation silently cut at 40 reads as
        # a conversation that ended.
        "truncated": truncated,
    }


# Aggregates, so they are the ORDER BY expressions rather than column names.
# started_at ascending is "oldest conversation first", which is how a backlog
# is worked; message_count descending finds the ones that have been going on.
_THREAD_SORTS = {
    "last_activity_at": "max(m.sent_at)",
    "started_at": "min(m.sent_at)",
    "message_count": "count(*)",
}


@router.get("/user/threads")
def list_threads(
    needs_attention: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    sort: str = Query(default="last_activity_at"),
    dir: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
):
    """Conversations, newest activity first.

    The list form of a thread rather than a list of messages. Grouping a page
    of messages client-side would produce partial threads at the page
    boundaries - a conversation cut in half by pagination reads as a
    conversation that is half that long, which is a lie exactly where nobody
    looks for one.

    Keyed on COALESCE(thread id, own message id), the same derivation the
    thread reader uses, so a conversation contains the message that started it.

    needs_attention is the correctable pile: a thread carrying job-related mail
    that reached no application. Not "unread" - we have no such concept and
    inventing one would be a second inbox to maintain.
    """
    where = ["m.user_id = %(user)s"]
    params: dict[str, Any] = {"user": user.id, "limit": limit, "offset": offset}
    if q:
        where.append("(m.subject ILIKE %(q)s OR m.from_email ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    predicate = " AND ".join(where)

    # One predicate, negated rather than written twice: two spellings of the
    # same condition is how a filter and its inverse stop being complements.
    _CORRECTABLE = (
        "bool_or(ce.kind IS NOT NULL AND ce.kind <> 'not_job_related' "
        "AND cm.application_id IS NULL)"
    )
    having = ""
    if needs_attention is True:
        having = f"HAVING {_CORRECTABLE}"
    elif needs_attention is False:
        having = f"HAVING NOT {_CORRECTABLE}"

    base = f"""
        FROM email_messages m
        LEFT JOIN (
            SELECT DISTINCT ON (message_id) message_id, kind FROM email_events
            ORDER BY message_id, id DESC
        ) ce ON ce.message_id = m.id
        LEFT JOIN (
            SELECT DISTINCT ON (message_id) message_id, application_id FROM application_matches
            ORDER BY message_id, id DESC
        ) cm ON cm.message_id = m.id
        WHERE {predicate}
        GROUP BY {_THREAD_KEY}
        {having}
    """
    total = db.query_one(f"SELECT count(*) AS n FROM (SELECT 1 {base}) s", params)
    # Whitelisted, because these are aggregates concatenated into SQL and an
    # unknown value must fall back rather than reach the database.
    order = _THREAD_SORTS.get(sort, _THREAD_SORTS["last_activity_at"])
    direction = "ASC" if dir == "asc" else "DESC"
    rows = db.query(
        f"""
        SELECT {_THREAD_KEY} AS thread_id,
               count(*) AS message_count,
               max(m.sent_at) AS last_activity_at,
               min(m.sent_at) AS started_at,
               (array_agg(m.subject ORDER BY m.sent_at) FILTER (WHERE m.subject IS NOT NULL))[1]
                   AS subject,
               array_agg(DISTINCT m.from_email) FILTER (WHERE m.from_email IS NOT NULL)
                   AS participants,
               array_agg(DISTINCT ce.kind) FILTER (WHERE ce.kind IS NOT NULL) AS kinds,
               count(*) FILTER (
                   WHERE ce.kind IS NOT NULL AND ce.kind <> 'not_job_related'
                     AND cm.application_id IS NULL
               ) > 0 AS needs_attention,
               (array_agg(m.id ORDER BY m.sent_at DESC))[1] AS latest_message_id,
               max(cm.application_id) AS application_id
        {base}
        ORDER BY {order} {direction} NULLS LAST
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    return {
        "threads": rows,
        "total": (total or {}).get("n", 0),
        "has_more": offset + len(rows) < (total or {}).get("n", 0),
    }
