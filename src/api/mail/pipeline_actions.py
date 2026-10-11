"""Event answers and the action items current mail evidence creates."""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel

from api import db
from api.mail.current import current_event, current_match

# Events that create something the user has to do, and what it is called.
# An ask belongs to an application. A recruiter approach is attached to none
# (match.UNATTACHABLE_KINDS), so it asks nothing here.
_EVENT_TO_ACTION = {
    "assessment_invite": "complete_assessment",
    "interview_invite": "schedule_interview",
    "info_request": "send_information",
    "offer": "respond_to_offer",
}

# A later event that settles an earlier ask. This is what makes the system
# no-touch: an assessment invite is closed by the acknowledgement that follows
# it, not by the user remembering to tick something off.
_RESOLVING_EVENTS = {
    "complete_assessment": ("acknowledgement", "interview_invite", "rejection", "position_closed"),
    "schedule_interview": ("interview_scheduled", "rejection", "position_closed"),
    "send_information": ("acknowledgement", "interview_invite", "rejection", "position_closed"),
    "respond_to_offer": ("rejection",),
}


def settles_on(kind: str) -> list[str]:
    """The event kinds that can close this ask, which is often none.

    `resolved_at IS NULL` currently means two different things and the
    difference is the whole product. An assessment invite from last week is
    outstanding: something can still arrive to settle it. An offer from 2020 is
    not outstanding - nothing was ever going to settle it, because no email
    says "you accepted". Measured over the corpus: of 71 applications carrying
    an offer event, only 11 have ANY later event at all, and no kind follows
    one reliably.

    So this is a property of the KIND, not of the item's age, and it is the
    honest thing to expose. A caller that sees an empty list knows the item is
    unresolved by construction rather than by neglect, and must not render it
    as a live obligation.
    """
    return list(_RESOLVING_EVENTS.get(kind, ()))


# What a person answered about an event, in `event_answers`, which is
# append-only: the newest answer to a question about an event is the one in
# force, so reopening an ask is an answer, not an erasure.
STATUS_QUESTION = "status"
ACTION_QUESTION = "action"
DONE = "done"
REOPENED = "reopened"


def answer(
    user_id: int, event_id: int, question: str, response: str, note: str | None = None
) -> None:
    db.execute(
        "INSERT INTO event_answers (event_id, question, answer, actor_user_id, note) "
        "VALUES (%s, %s, %s, %s, %s)",
        (event_id, question, response, user_id, note),
    )


def latest_answers(question: str, event_ids: list[int]) -> dict[int, dict[str, Any]]:
    return {
        row["event_id"]: row
        for row in db.query(
            "SELECT DISTINCT ON (event_id) event_id, answer, note, actor_user_id, created_at "
            "FROM event_answers WHERE question = %s AND event_id = ANY(%s) "
            "ORDER BY event_id, id DESC",
            (question, event_ids),
        )
    }


class ActionItem(BaseModel):
    """An ask a message made, derived when read and never stored.

    Its id is the event's id: one event asks one thing. Open until a later
    event on the same application settles it or the person marks it done; a
    reopen is a later answer. It used to be a table kept in step by a sweep
    over every application, which left a row behind when its message moved
    (217 of them, closed as "no longer part of this application"); an ask
    read from the current rows moves with the message instead."""

    id: int
    user_id: int
    application_id: int
    event_id: int
    kind: str
    due_at: datetime.datetime | None
    resolved_at: datetime.datetime | None
    resolution: str | None
    resolved_by_event_id: int | None
    created_at: datetime.datetime
    settles_on: list[str]
    message_id: int
    subject: str | None
    from_email: str | None
    sent_at: datetime.datetime | None


def action_items(
    user_id: int, application_id: int | None = None, *, open_only: bool = False
) -> list[ActionItem]:
    """The asks on a person's applications, or on one of them."""
    rows = db.query(
        f"""
        WITH cm AS ({current_match("application_id")}),
        ce AS ({current_event("id", "kind", "deadline_at", "created_at")})
        SELECT cm.application_id, e.id, e.kind, e.deadline_at, e.created_at,
               m.id AS message_id, m.subject, m.from_email, m.sent_at
        FROM cm
        JOIN ce e ON e.message_id = cm.message_id
        JOIN email_messages m ON m.id = cm.message_id
        JOIN applications a ON a.id = cm.application_id
        WHERE a.user_id = %(user)s
          AND (%(app)s::bigint IS NULL OR a.id = %(app)s)
        ORDER BY cm.application_id, e.id
        """,
        {"user": user_id, "app": application_id},
    )
    asks = [r for r in rows if r["kind"] in _EVENT_TO_ACTION]
    answers = latest_answers(ACTION_QUESTION, [r["id"] for r in asks])
    by_app: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_app.setdefault(row["application_id"], []).append(row)
    items = []
    for ask in asks:
        kind = _EVENT_TO_ACTION[ask["kind"]]
        settling = _RESOLVING_EVENTS.get(kind, ())
        later = [
            e
            for e in by_app[ask["application_id"]]
            if e["kind"] in settling and e["id"] > ask["id"]
        ]
        settled = min(later, key=lambda e: e["id"]) if later else None
        done = answers.get(ask["id"])
        if done is not None and done["answer"] != DONE:
            done = None
        if settled is not None:
            resolved_at, resolution = settled["created_at"], f"superseded by {settled['kind']}"
        elif done is not None:
            resolved_at, resolution = done["created_at"], done["note"] or "marked done"
        else:
            resolved_at, resolution = None, None
        if open_only and resolved_at is not None:
            continue
        items.append(
            ActionItem(
                id=ask["id"],
                user_id=user_id,
                application_id=ask["application_id"],
                event_id=ask["id"],
                kind=kind,
                due_at=ask["deadline_at"],
                resolved_at=resolved_at,
                resolution=resolution,
                resolved_by_event_id=settled["id"] if settled else None,
                created_at=ask["created_at"],
                settles_on=list(settling),
                message_id=ask["message_id"],
                subject=ask["subject"],
                from_email=ask["from_email"],
                sent_at=ask["sent_at"],
            )
        )
    items.sort(key=lambda i: (i.due_at is None, i.due_at or i.created_at, i.id))
    return items


def action_item(user_id: int, event_id: int) -> ActionItem | None:
    """The ask an event makes, if it makes one on this person's application."""
    row = db.query_one(
        f"""
        SELECT cm.application_id FROM email_events e
        JOIN ({current_match("application_id")}) cm ON cm.message_id = e.message_id
        WHERE e.id = %s
        """,
        (event_id,),
    )
    if row is None or row["application_id"] is None:
        return None
    return next((i for i in action_items(user_id, row["application_id"]) if i.id == event_id), None)
