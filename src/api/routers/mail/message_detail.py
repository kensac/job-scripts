"""A person's message and its append-only decision history."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.mail.store import owned_message
from api.problem import refuse
from api.routers.mail.shared import (
    Reclassification,
    Reclassified,
    Reverted,
    apply_classification,
    apply_revert,
)
from core.answers import EVENT_KINDS
from core.mail.html import sanitise

router = APIRouter()


class MailEvent(BaseModel):
    """One classification of this message, as written. The log is append-only
    and the newest wins, so a message with three of these was corrected twice.
    A null `model` is how a human correction is told apart from a model's."""

    id: int
    kind: str
    confidence: str | None
    occurred_at: datetime.datetime | None
    deadline_at: datetime.datetime | None
    deadline_inferred: bool
    detail: dict[str, Any] | None
    model: str | None
    created_at: datetime.datetime


class MailMatchRow(BaseModel):
    """One attempt to place this message, with the application it named."""

    id: int
    application_id: int | None
    method: str
    confidence: str | None
    rationale: str | None
    created_at: datetime.datetime
    company_name: str | None
    title: str | None


class ReadableMessage(BaseModel):
    """A message as it can be shown to the person it belongs to.

    `body_html` is SANITISED on read and the stored markup never leaves the
    server: a caller holding the raw markup will eventually render it, and the
    sandboxed iframe on the other side is only the second of two layers.
    `blocked_remote_content` says what was withheld, so a reader offering
    "load images" knows whether there is anything to load."""

    id: int
    subject: str | None
    from_email: str | None
    sent_at: datetime.datetime | None
    source: str
    body_text: str | None
    body_html: str | None
    blocked_remote_content: int


class MailMessageDetail(ReadableMessage):
    """The whole message with its history, for when the excerpt is not enough.

    Read-only and user-scoped. The body is already stored - withholding it
    would mean leaving for a mail client to check a decision this system
    made, which is the same as not being able to check it."""

    provider_message_id: str
    provider_thread_id: str | None
    from_name: str | None
    to_emails: list[str] | None
    prefilter_hit: bool | None
    prefilter_reason: str | None
    events: list[MailEvent]
    matches: list[MailMatchRow]


@router.get("/user/messages/{message_id}")
def message_detail(message_id: int, user: AuthedUser = Depends(require_user)) -> MailMessageDetail:
    """The whole message, for when the excerpt is not enough.

    Read-only and user-scoped. The body is already stored - withholding it
    would mean a person has to leave for their mail client to check a decision
    this system made, which is the same as not being able to check it.
    """
    row = db.query_one(
        "SELECT id, provider_message_id, provider_thread_id, source, from_email, from_name, "
        "to_emails, subject, sent_at, body_text, body_html, prefilter_hit, prefilter_reason "
        "FROM email_messages WHERE id = %s AND user_id = %s",
        (message_id, user.id),
    )
    if row is None:
        raise refuse(404, "NOT_FOUND", "message not found")
    # Sanitised on READ, never stored sanitised: body_html stays the message as
    # it arrived, so a better sanitiser improves every message ever received
    # rather than only the ones that come next. The raw markup is deliberately
    # NOT returned - a caller that has it will eventually render it.
    return MailMessageDetail(
        **readable_message_fields(row),
        events=db.query_as(
            MailEvent,
            "SELECT id, kind, confidence, occurred_at, deadline_at, deadline_inferred, detail, "
            "model, created_at FROM email_events WHERE message_id = %s ORDER BY id",
            (message_id,),
        ),
        matches=db.query_as(
            MailMatchRow,
            """
            SELECT am.id, am.application_id, am.method, am.confidence, am.rationale,
                   am.created_at, a.company_name, a.title
            FROM application_matches am
            LEFT JOIN applications a ON a.id = am.application_id
            WHERE am.message_id = %s ORDER BY am.id
            """,
            (message_id,),
        ),
    )


@router.post("/user/messages/{message_id}/classify")
def correct_classification(
    message_id: int, body: Reclassification, user: AuthedUser = Depends(require_user)
) -> Reclassified:
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
    message = owned_message(message_id, user.id)
    return apply_classification(message, body, actor_user_id=user.id)


class MessageKinds(BaseModel):
    kinds: list[str]


@router.get("/user/message-kinds")
def message_kinds(user: AuthedUser = Depends(require_user)) -> MessageKinds:
    """The vocabulary, served rather than copied.

    The client kept this list in two places and it drifts the moment a kind is
    added - the same failure as the stage vocabulary, which had a terminal
    state the frontend did not know about.
    """
    return MessageKinds(kinds=sorted(EVENT_KINDS))


@router.post("/user/messages/{message_id}/classify/revert", response_model_exclude_none=True)
def revert_classification(message_id: int, user: AuthedUser = Depends(require_user)) -> Reverted:
    """Undo a correction by restoring what the model last said.

    Another append, not a delete: a mis-correction has to be recoverable and
    the log still has to show that both happened. Refused when the model has
    never classified this message, because there is nothing to restore.
    """
    owned_message(message_id, user.id)
    return apply_revert(message_id, actor_user_id=user.id)


def readable_message_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Swap stored markup for display-safe markup, and say what was withheld.

    The raw html never leaves the server: a caller holding it will eventually
    render it, and the sandboxed iframe on the other side is only the second
    of two layers.
    """
    raw = row.pop("body_html", None)
    safe, blocked = sanitise(raw) if raw else (None, 0)
    return {**row, "body_html": safe, "blocked_remote_content": blocked}
