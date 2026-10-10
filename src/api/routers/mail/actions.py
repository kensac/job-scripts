"""What the mail asks of a person: proposals to confirm, actions to close.

Both are derived from the same event stream the pipeline reads, and neither is
a second inbox. A proposal is where the mail and the board disagree; an action
is the one kind of item nothing else will ever settle.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.auth import AuthedUser, require_user
from api.mail import pipeline as mail_pipeline
from api.mail.pipeline import Proposal, ProposalAnswered
from api.models import Ok
from api.problem import refuse
from api.routers.mail.shared import Evidence, _evidence_for

router = APIRouter()


class SuggestionAnswer(BaseModel):
    response: str
    note: str | None = None


class Suggestion(Proposal):
    """A proposal with what it rests on. The queue carries the summary
    instead: the body is a detail view's worth of payload and there are 1,159
    of these."""

    evidence: Evidence | None


class Suggestions(BaseModel):
    suggestions: list[Suggestion]
    total: int


class ActionClosed(BaseModel):
    """`already` says the item was resolved before this call, so a second
    click is not reported as a second resolution."""

    ok: bool
    already: bool


@router.get("/user/suggestions")
def suggestions(user: AuthedUser = Depends(require_user)) -> Suggestions:
    """Where the mail and the board disagree, as things to confirm.

    The derivation lives in `mail_pipeline.proposals_for`, because the review
    queue asks the same question and one of the two spellings would drift. What
    this route adds is EVIDENCE - the message, the sender, and where the
    company appears in the body - because a proposal a person cannot check is
    one they have to take on faith. The queue carries the summary instead; the
    body is a detail view's worth of payload and there are 1,159 of these.
    """
    rows = mail_pipeline.proposals_for(user.id)
    evidence = _evidence_for(sorted({r.message_id for r in rows}))
    return Suggestions(
        suggestions=[
            Suggestion(**row.model_dump(), evidence=evidence.get(row.message_id)) for row in rows
        ],
        total=len(rows),
    )


@router.post("/user/suggestions/{application_id}/{event_id}")
def answer_suggestion(
    application_id: int,
    event_id: int,
    body: SuggestionAnswer,
    user: AuthedUser = Depends(require_user),
) -> ProposalAnswered:
    """Accept a proposal and the board moves; dismiss it and it stays put.

    Reports what it actually wrote. It used to return the proposed status
    whenever the answer was `accepted`, including for the 1,817 applications
    with no board row, where the UPDATE matched nothing - so the caller was
    told a status had moved that no SELECT could find.
    """
    if body.response not in (mail_pipeline.ACCEPTED, mail_pipeline.DISMISSED):
        raise refuse(
            400,
            "INVALID_RESPONSE",
            f"response must be {mail_pipeline.ACCEPTED} or {mail_pipeline.DISMISSED}",
        )
    answered = mail_pipeline.answer_proposal(user.id, application_id, event_id, body.response)
    if answered is None:
        raise refuse(404, "NOT_FOUND", "no suggestion for that event")
    return answered


class ActionAnswer(BaseModel):
    note: str | None = None


@router.post("/user/actions/{action_id}/resolve")
def resolve_action(
    action_id: int, body: ActionAnswer, user: AuthedUser = Depends(require_user)
) -> ActionClosed:
    """Mark an action done, because for some kinds nothing else ever will.

    Auto-resolution carries most of the weight and should: an assessment invite
    is closed by the acknowledgement that follows it, not by the user
    remembering. That is what makes this no-touch rather than a second inbox.

    But `respond_to_offer` closes only on a rejection, so accepting an offer,
    declining it or signing never settles it: 146 open and none has ever
    closed. For that, a person is the only producer, exactly as the board is
    the only producer of `withdrawn`.

    `action_id` is the id of the event that asked. The answer is appended to
    `event_answers`, the record of what a person said.
    """
    item = mail_pipeline.action_item(user.id, action_id)
    if item is None:
        raise refuse(404, "NOT_FOUND", "action not found")
    if item.resolved_at is not None:
        return ActionClosed(ok=True, already=True)
    mail_pipeline.answer(
        user.id, action_id, mail_pipeline.ACTION_QUESTION, mail_pipeline.DONE, body.note
    )
    return ActionClosed(ok=True, already=False)


@router.post("/user/actions/{action_id}/reopen")
def reopen_action(
    action_id: int, body: ActionAnswer, user: AuthedUser = Depends(require_user)
) -> Ok:
    """Undo a manual resolution.

    Refused on one that a later event settled: that is a fact about the mail
    rather than a decision the user made, and reopening it would change
    nothing: the event still settles it. A reopen is appended, so the closing
    answer stays readable underneath it.
    """
    item = mail_pipeline.action_item(user.id, action_id)
    if item is None:
        raise refuse(404, "NOT_FOUND", "action not found")
    if item.resolved_by_event_id is not None:
        raise refuse(
            409,
            "SETTLED_BY_MAIL",
            "a later email settled this; it would close again on the next pass",
        )
    if item.resolved_at is not None:
        mail_pipeline.answer(
            user.id, action_id, mail_pipeline.ACTION_QUESTION, mail_pipeline.REOPENED, body.note
        )
    return Ok()
