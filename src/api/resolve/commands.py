from __future__ import annotations

from fastapi import HTTPException

from api import db
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.resolve.contracts import (
    ACCEPT_STATUS,
    ASSIGN,
    CONFIRM_MATCH,
    DECLINE_STATUS,
    MARK_DONE,
    NOT_AN_APPLICATION,
    NOT_JOB_RELATED,
    REJECT_MATCH,
    ResolveRequest,
    ResolveResult,
)


def _owned_message(message_id: int, owner_id: int) -> None:
    row = db.query_one(
        "SELECT id FROM email_messages WHERE id = %s AND user_id = %s", (message_id, owner_id)
    )
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})


def _resolve_message(
    message_id: int, body: ResolveRequest, owner_id: int, actor_user_id: int
) -> ResolveResult:
    _owned_message(message_id, owner_id)

    if body.choice == ASSIGN:
        if body.target is None:
            raise HTTPException(
                400, detail={"code": "TARGET_REQUIRED", "message": "assign needs an application"}
            )
        # `dismissed_at IS NULL` is the same predicate the verb's eligibility is
        # declared from, ENFORCED HERE rather than only announced. Without it
        # the server said "no application at this company yet" for a dismissed
        # one and then accepted it as a target anyway, so the declaration was
        # decoration: a client that ignored `eligible` got its way, and mail
        # landed on an application whose whole meaning is that it should never
        # have existed.
        owner = db.query_one(
            "SELECT id, dismissed_at FROM applications WHERE id = %s AND user_id = %s",
            (body.target, owner_id),
        )
        if owner is None:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown application"})
        if owner["dismissed_at"] is not None:
            # 409 rather than 404: it exists and the caller may see it, but the
            # state it is in refuses the verb, and saying so is what lets them
            # restore it instead of guessing at a missing row.
            raise HTTPException(
                409,
                detail={
                    "code": "DISMISSED",
                    "message": "that application is dismissed; restore it before assigning to it",
                },
            )
        mail_match.record(
            message_id,
            mail_match.Match(
                body.target, mail_match.MANUAL, "high", body.note or "resolved by hand"
            ),
            actor_user_id=actor_user_id,
        )
        mail_pipeline.sync_action_items(body.target)
        return ResolveResult(ok=True, choice=body.choice, application_id=body.target)

    if body.choice == NOT_AN_APPLICATION:
        # Recorded as the matcher's own refusal, so every reader that already
        # tells a refusal from a failure sees it without learning a new value.
        mail_match.record(
            message_id,
            mail_match.Match(
                None, mail_match.NOT_AN_APPLICATION, "high", body.note or "refused by hand"
            ),
            actor_user_id=actor_user_id,
        )
        return ResolveResult(ok=True, choice=body.choice)

    # not_job_related: an append to the event log, the same retraction rule a
    # reclassification uses. The match is retracted too, because an event that
    # says this is not job mail cannot leave the message attached to a job.
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model, actor_user_id) "
        "VALUES (%s, 'not_job_related', 'high', %s, NULL, %s)",
        (message_id, db.jsonb({"corrected_by_user": True}), actor_user_id),
    )
    mail_match.record(
        message_id,
        mail_match.Match(None, mail_match.NOT_AN_APPLICATION, "high", "retracted: not job mail"),
        actor_user_id=actor_user_id,
    )
    return ResolveResult(ok=True, choice=body.choice)


def _resolve_match(
    match_id: int, body: ResolveRequest, owner_id: int, actor_user_id: int
) -> ResolveResult:
    """Confirm or reject one attachment.

    Bound to the OWNER through the application, not just to the match id.
    Owning a parent says nothing about owning a child, and a match id taken
    from the request and trusted would let anyone's attachment be answered.

    Answering the row that is no longer current is refused rather than
    silently applied. A queue page can be minutes old, and confirming an
    attachment that has since been replaced would write back a decision about
    a world that is gone.
    """
    row = db.query_one(
        """
        SELECT am.id, am.message_id, am.application_id, am.method, am.confidence,
               (am.id = (SELECT max(id) FROM application_matches
                         WHERE message_id = am.message_id)) AS is_current
        FROM application_matches am
        JOIN applications a ON a.id = am.application_id
        WHERE am.id = %s AND a.user_id = %s
        """,
        (match_id, owner_id),
    )
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
    if not row["is_current"]:
        raise HTTPException(
            409,
            detail={
                "code": "STALE",
                "message": "this attachment has already been superseded; reload the queue",
            },
        )

    if body.choice == CONFIRM_MATCH:
        mail_match.confirm(row["message_id"], row, actor_user_id=actor_user_id, note=body.note)
        return ResolveResult(ok=True, choice=body.choice, application_id=row["application_id"])

    mail_match.reject(row["message_id"], actor_user_id=actor_user_id, note=body.note)
    # The events this message carried stop reaching the application, so
    # anything they opened has to follow rather than sit there asking about an
    # application it is no longer part of.
    mail_pipeline.sync_action_items(row["application_id"])
    return ResolveResult(ok=True, choice=body.choice)


def _resolve_proposal(
    application_id: int, event_id: int, body: ResolveRequest, owner_id: int
) -> ResolveResult:
    answered = mail_pipeline.answer_proposal(
        owner_id,
        application_id,
        event_id,
        mail_pipeline.ACCEPTED if body.choice == ACCEPT_STATUS else mail_pipeline.DISMISSED,
    )
    if answered is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
    return ResolveResult(
        ok=True,
        choice=body.choice,
        application_id=application_id,
        board_updated=answered.board_updated,
        board_status=answered.board_status,
        reason=answered.reason,
    )


def _resolve_action(action_id: int, body: ResolveRequest, owner_id: int) -> ResolveResult:
    row = db.query_one(
        "SELECT id, resolved_at FROM action_items WHERE id = %s AND user_id = %s",
        (action_id, owner_id),
    )
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
    if row["resolved_at"] is None:
        db.execute(
            "UPDATE action_items SET resolved_at = now(), resolution = %s WHERE id = %s",
            (body.note or "marked done", action_id),
        )
    return ResolveResult(ok=True, choice=body.choice)


# Which verbs each item kind accepts. Declared once and checked here, so a verb
# offered on the wrong kind is a 400 rather than an operation that half runs.
_CHOICES_BY_KIND = {
    "message": {ASSIGN, NOT_AN_APPLICATION, NOT_JOB_RELATED},
    "match": {CONFIRM_MATCH, REJECT_MATCH},
    "proposal": {ACCEPT_STATUS, DECLINE_STATUS},
    "action": {MARK_DONE},
}


def resolve(item_id: str, body: ResolveRequest, owner_id: int, actor_user_id: int) -> ResolveResult:
    kind, _, raw = item_id.partition(":")
    parts = raw.split(":")
    if kind not in _CHOICES_BY_KIND or not all(p.isdigit() for p in parts):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
    if body.choice not in _CHOICES_BY_KIND[kind]:
        raise HTTPException(
            400,
            detail={
                "code": "WRONG_CHOICE",
                "message": f"{body.choice} is not a verb on a {kind} item",
            },
        )

    if kind == "message" and len(parts) == 1:
        return _resolve_message(int(parts[0]), body, owner_id, actor_user_id)
    if kind == "match" and len(parts) == 1:
        return _resolve_match(int(parts[0]), body, owner_id, actor_user_id)
    if kind == "proposal" and len(parts) == 2:
        return _resolve_proposal(int(parts[0]), int(parts[1]), body, owner_id)
    if kind == "action" and len(parts) == 1:
        return _resolve_action(int(parts[0]), body, owner_id)
    raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
