"""What the mail asks of a person: proposals to confirm, actions to close.

Both are derived from the same event stream the pipeline reads, and neither is
a second inbox. A proposal is where the mail and the board disagree; an action
is the one kind of item nothing else will ever settle.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.mail import pipeline as mail_pipeline
from api.routers.mail.shared import _evidence_for

router = APIRouter()


class SuggestionAnswer(BaseModel):
    response: str
    note: str | None = None


@router.get("/user/suggestions")
def suggestions(user: AuthedUser = Depends(require_user)):
    """Where the mail and the board disagree, as things to confirm.

    The derivation lives in `mail_pipeline.proposals_for`, because the review
    queue asks the same question and one of the two spellings would drift. What
    this route adds is EVIDENCE - the message, the sender, and where the
    company appears in the body - because a proposal a person cannot check is
    one they have to take on faith. The queue carries the summary instead; the
    body is a detail view's worth of payload and there are 1,159 of these.
    """
    rows = mail_pipeline.proposals_for(user.id)
    evidence = _evidence_for(sorted({r["message_id"] for r in rows}))
    return {
        "suggestions": [{**row, "evidence": evidence.get(row["message_id"])} for row in rows],
        "total": len(rows),
    }


@router.post("/user/suggestions/{application_id}/{event_id}")
def answer_suggestion(
    application_id: int,
    event_id: int,
    body: SuggestionAnswer,
    user: AuthedUser = Depends(require_user),
):
    """Accept a proposal and the board moves; dismiss it and it stays put.

    Reports what it actually wrote. It used to return the proposed status
    whenever the answer was `accepted`, including for the 1,817 applications
    with no board row, where the UPDATE matched nothing - so the caller was
    told a status had moved that no SELECT could find.
    """
    if body.response not in (mail_pipeline.ACCEPTED, mail_pipeline.DISMISSED):
        raise HTTPException(
            status_code=400,
            detail=f"response must be {mail_pipeline.ACCEPTED} or {mail_pipeline.DISMISSED}",
        )
    answered = mail_pipeline.answer_proposal(user.id, application_id, event_id, body.response)
    if answered is None:
        raise HTTPException(status_code=404, detail="no suggestion for that event")
    return answered


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
