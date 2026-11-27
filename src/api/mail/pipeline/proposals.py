"""Proposals to reconcile mail outcomes with a person's board."""

from __future__ import annotations

import datetime

from pydantic import BaseModel

from api import db
from api.mail.current import current_event, current_match
from api.mail.pipeline.actions import STATUS_QUESTION, answer


class ProposedFrom(BaseModel):
    """The row `proposals_for` selects: an application, the latest event that
    is evidence about it, and the message that event was read from.

    `board_updatable` is the LEFT JOIN made explicit. 1,817 of 2,543
    applications here have no board row, because mail predating the catalog is
    the normal case, and answering a proposal on one of those moves nothing.
    Declared so a caller can say that before the click rather than report a
    status it did not write afterwards."""

    application_id: int
    company_name: str | None
    title: str | None
    job_id: int | None
    board_status: str | None
    date_applied: datetime.date | None
    board_updatable: bool
    event_id: int
    kind: str
    company: str | None
    role_title: str | None
    message_id: int
    subject: str | None
    from_email: str | None
    sent_at: datetime.datetime | None


class Proposal(ProposedFrom):
    """A proposal as offered. `suggested_status` is what the event kind means
    for the board; `board_reason` says why answering will move nothing, and is
    null when it will."""

    suggested_status: str
    board_reason: str | None


class ProposalAnswered(BaseModel):
    """What answering actually wrote.

    `board_status` is present only when a row moved. It used to carry the
    proposed status whenever the answer was `accepted`, including for the
    applications with no board row where the UPDATE matched nothing, so the
    caller was told a status had moved that no SELECT could find."""

    ok: bool
    response: str
    board_updated: bool
    board_status: str | None
    # Complete sentences rendered verbatim by the client; None when a row moved.
    reason: str | None


# Event kinds that imply a board status change (excludes acknowledgements).
STATUS_FROM_EVENT = {
    "rejection": "Rejected",
    "position_closed": "No Longer Available",
    "offer": "Offer",
    "interview_invite": "Interviewing",
    "interview_scheduled": "Interviewing",
}

# "Still waiting" statuses: once the user has moved a row on, the mail only
# confirms what they already know, so there is nothing to propose.
UNRESOLVED_BOARD_STATUSES = ("Application Submitted", "Follow-up")

NOT_ON_BOARD = "This application is not on your board, so there is no status to move."

ACCEPTED = "accepted"
DISMISSED = "dismissed"


def proposals_for(user_id: int) -> list[Proposal]:
    """Derive unanswered status proposals where mail disagrees with an unresolved
    or absent board row. Never overwrites `user_jobs.status`, which the user typed.

    LEFT JOIN so mail-only applications (without a `user_jobs` row) still surface
    with `board_updatable=False`: 1,817 of 2,543 applications had no board row,
    and the inner join silenced 947 of 1,159 proposals.
    """
    rows = db.query_as(
        ProposedFrom,
        f"""
        WITH current_match AS (
            {current_match("application_id")}
        ),
        current_event AS (
            {current_event("id", "kind", "detail")}
        )
        SELECT DISTINCT ON (a.id, e.kind)
               a.id AS application_id, a.company_name, a.title, a.job_id,
               uj.status AS board_status,
               (a.applied_at AT TIME ZONE 'UTC')::date AS date_applied,
               uj.user_id IS NOT NULL AS board_updatable,
               e.id AS event_id, e.kind,
               e.detail->>'company' AS company, e.detail->>'role_title' AS role_title,
               m.id AS message_id, m.subject, m.from_email, m.sent_at
        FROM applications a
        LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
        JOIN current_match cm ON cm.application_id = a.id
        JOIN current_event e ON e.message_id = cm.message_id
        JOIN email_messages m ON m.id = cm.message_id
        WHERE a.user_id = %(user)s
          AND a.dismissed_at IS NULL
          AND (uj.user_id IS NULL OR uj.status = ANY(%(unresolved)s))
          AND e.kind = ANY(%(kinds)s)
          AND NOT EXISTS (
              SELECT 1 FROM event_answers ea
              WHERE ea.event_id = e.id AND ea.question = %(status)s
          )
        ORDER BY a.id, e.kind, e.id DESC
        """,
        {
            "user": user_id,
            "unresolved": list(UNRESOLVED_BOARD_STATUSES),
            "kinds": sorted(STATUS_FROM_EVENT),
            "status": STATUS_QUESTION,
        },
    )
    return [
        Proposal(
            **row.model_dump(),
            suggested_status=STATUS_FROM_EVENT[row.kind],
            board_reason=None if row.board_updatable else NOT_ON_BOARD,
        )
        for row in rows
    ]


def answer_proposal(
    user_id: int, application_id: int, event_id: int, response: str
) -> ProposalAnswered | None:
    """Record a person's answer against `event_id` and update `user_jobs.status`
    when accepted and a board row exists.

    Returns None if the application is not owned by `user_id` or `event_id` does
    not belong to a message currently matched to `application_id`; checking
    ownership alone would let another user's event decide this status.

    Answers key on the event, so a dismissal silences this evidence only and a
    later event asks again.
    """
    app = db.query_one(
        "SELECT id, job_id FROM applications WHERE id = %s AND user_id = %s",
        (application_id, user_id),
    )
    if app is None:
        return None
    event = db.query_one(
        f"""
        WITH current_match AS (
            {current_match("application_id")}
        )
        SELECT e.id, e.kind
        FROM email_events e
        JOIN current_match cm ON cm.message_id = e.message_id
        WHERE e.id = %s AND cm.application_id = %s
        """,
        (event_id, application_id),
    )
    if event is None or event["kind"] not in STATUS_FROM_EVENT:
        return None

    status = STATUS_FROM_EVENT[event["kind"]]
    answer(user_id, event_id, STATUS_QUESTION, response)
    updated = 0
    if response == ACCEPTED and app["job_id"] is not None:
        # Check actual rowcount: applications.job_id survives user_jobs deletion.
        updated = db.execute_count(
            "UPDATE user_jobs SET status = %s, person_touched_at = now(), updated_at = now() "
            "WHERE user_id = %s AND job_id = %s",
            (status, user_id, app["job_id"]),
        )
    return ProposalAnswered(
        ok=True,
        response=response,
        board_updated=bool(updated),
        board_status=status if updated else None,
        # Complete sentences rendered verbatim by the client.
        reason=None
        if updated
        else (
            "Recorded. This application is not on your board, so no status moved."
            if response == ACCEPTED
            else "Recorded. Declining moves nothing, by design."
        ),
    )
