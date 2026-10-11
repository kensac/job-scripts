"""Person corrections to application matches and dismissals."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.mail import match as mail_match
from api.models import Ok
from api.problem import refuse

router = APIRouter()


class Correction(BaseModel):
    note: str | None = None


class MatchMoved(BaseModel):
    """Which message the correction moved. The append is on the MESSAGE, so
    the id says what changed hands rather than restating the application the
    caller already named."""

    ok: bool
    message_id: int


def owned_pipeline_application(application_id: int, user_id: int) -> dict[str, Any]:
    app = db.query_one(
        "SELECT id, source_provenance, dismissed_at FROM applications "
        "WHERE id = %s AND user_id = %s",
        (application_id, user_id),
    )
    if app is None:
        raise refuse(404, "NOT_FOUND", "application not found")
    return app


@router.post("/user/pipeline/{application_id}/matches/{match_id}/detach")
def detach_match(
    application_id: int,
    match_id: int,
    body: Correction,
    user: AuthedUser = Depends(require_user),
) -> MatchMoved:
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
    owned_pipeline_application(application_id, user.id)
    match = db.query_one(
        "SELECT id, message_id FROM application_matches WHERE id = %s AND application_id = %s",
        (match_id, application_id),
    )
    if match is None:
        raise refuse(404, "NOT_FOUND", "match not found on this application")
    mail_match.reject(
        match["message_id"], actor_user_id=user.id, note=body.note or "detached by the user"
    )
    return MatchMoved(ok=True, message_id=match["message_id"])


@router.post("/user/pipeline/{application_id}/matches/{match_id}/reattach")
def reattach_match(
    application_id: int,
    match_id: int,
    body: Correction,
    user: AuthedUser = Depends(require_user),
) -> MatchMoved:
    """Undo a detach, by the same append."""
    owned_pipeline_application(application_id, user.id)
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
        raise refuse(404, "NOT_FOUND", "match not found on this application")
    mail_match.record(
        match["message_id"],
        mail_match.Match(
            application_id, mail_match.MANUAL, "high", body.note or "reattached by the user"
        ),
        actor_user_id=user.id,
    )
    return MatchMoved(ok=True, message_id=match["message_id"])


@router.post("/user/pipeline/{application_id}/dismiss")
def dismiss_application(
    application_id: int, body: Correction, user: AuthedUser = Depends(require_user)
) -> Ok:
    """This application should never have existed.

    Different from detaching a message: no correction to the matches fixes an
    application that mail invented, because removing one message leaves a
    shell that still has a stage.

    Refused on a tracker application. That row exists because the user entered
    it; mail evidence did not create it and no mail correction may remove it.
    Accepting the call would imply the mail pipeline owns something it does
    not.
    """
    app = owned_pipeline_application(application_id, user.id)
    if app["source_provenance"] != "email":
        raise refuse(
            409,
            "TRACKER_APPLICATION",
            "only a mail-derived application can be dismissed; this one came from the tracker",
        )
    db.execute(
        "UPDATE applications SET dismissed_at = now(), dismissed_reason = %s, updated_at = now() "
        "WHERE id = %s",
        (body.note, application_id),
    )
    return Ok()


@router.post("/user/pipeline/{application_id}/restore")
def restore_application(
    application_id: int, body: Correction, user: AuthedUser = Depends(require_user)
) -> Ok:
    owned_pipeline_application(application_id, user.id)
    db.execute(
        "UPDATE applications SET dismissed_at = NULL, dismissed_reason = NULL, "
        "updated_at = now() WHERE id = %s",
        (application_id,),
    )
    return Ok()
