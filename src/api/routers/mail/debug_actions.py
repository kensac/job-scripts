"""Administrative corrections to append-only mail verdicts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api import db
from api.auth import AuthedUser
from api.mail import match as mail_match
from api.problem import refuse
from api.routers.admin import require_admin
from api.routers.mail.debug_models import MatchOverridden, MatchOverride
from api.routers.mail.shared import (
    Candidates,
    Reclassification,
    Reclassified,
    Reverted,
    apply_classification,
    apply_revert,
    candidates_payload,
)

router = APIRouter()


def _admin_message(message_id: int) -> dict[str, Any]:
    """Any user's message, for an administrator.

    Deliberately NOT `store.owned_message`: the whole point of these routes is that
    the admin corrects other people's mailboxes - friends and family, not a
    hypothetical. The ownership rule that applies is `require_admin` on the
    route; what this returns is the OWNER, because every helper below needs to
    be scoped to them rather than to the caller.
    """
    message = db.query_one(
        "SELECT id, user_id, subject, from_email, sent_at, body_text, provider_thread_id "
        "FROM email_messages WHERE id = %s",
        (message_id,),
    )
    if message is None:
        raise refuse(404, "NOT_FOUND", "unknown message")
    return message


@router.get("/admin/mail/{message_id}/candidates")
def admin_match_candidates(
    message_id: int,
    q: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    user: AuthedUser = Depends(require_admin),
) -> Candidates:
    """The same picker the user gets, over the message owner's applications.

    The admin panel offered a bare application-id field, which requires
    knowing an id that is not displayed anywhere - so the only correction
    available in practice was no correction. This is the identical ranking the
    user side computes, including the candidates `by_company` refused to
    choose between, which are the ones a person is best placed to settle.
    """
    message = _admin_message(message_id)
    return candidates_payload(message, message["user_id"], q, limit)


@router.post("/admin/mail/{message_id}/classify")
def admin_correct_classification(
    message_id: int, body: Reclassification, user: AuthedUser = Depends(require_admin)
) -> Reclassified:
    """Correct what a message IS, in someone else's mailbox.

    Recorded against the admin rather than the owner: `actor_user_id` is the
    caller, so a later reader can tell an administrator's correction from the
    owner's own without a second flag saying which.
    """
    message = _admin_message(message_id)
    return apply_classification(message, body, actor_user_id=user.id)


@router.post("/admin/mail/{message_id}/classify/revert", response_model_exclude_none=True)
def admin_revert_classification(
    message_id: int, user: AuthedUser = Depends(require_admin)
) -> Reverted:
    """Undo a correction by restoring the model's last answer."""
    _admin_message(message_id)
    return apply_revert(message_id, actor_user_id=user.id)


@router.post("/admin/mail/{message_id}/match")
def override_match(
    message_id: int, body: MatchOverride, user: AuthedUser = Depends(require_admin)
) -> MatchOverridden:
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
        raise refuse(404, "NOT_FOUND", "unknown message")
    if body.application_id is not None:
        owner = db.query_one(
            "SELECT user_id FROM applications WHERE id = %s", (body.application_id,)
        )
        if not owner or owner["user_id"] != message["user_id"]:
            # Cross-user match would attribute one person's outcome to
            # another's application. 404 rather than 403: whether that
            # application exists is not something the caller is entitled to.
            raise refuse(404, "NOT_FOUND", "unknown application")
    # A deliberate refusal is recorded as the matcher's own refusal method, so
    # every reader that already distinguishes the two - the unmatched cut, the
    # analytics breakdown, the pipeline filter - sees it without being taught a
    # fourth value. Agreeing with the matcher should look like agreeing with it.
    if body.application_id is None and body.outcome == "not_an_application":
        method, confidence = mail_match.NOT_AN_APPLICATION, "high"
    else:
        method = mail_match.MANUAL
        confidence = "high" if body.application_id is not None else "none"
    mail_match.record(
        message_id,
        actor_user_id=user.id,
        match=mail_match.MatchDecision(
            body.application_id, method, confidence, f"set by admin {user.sub}"
        ),
    )
    return MatchOverridden(ok=True, current=mail_match.latest(message_id))
