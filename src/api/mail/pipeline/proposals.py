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
    # Whole sentences, capitalised and stopped: the client renders this
    # verbatim rather than composing its own line, so a lowercase fragment
    # would land mid-paragraph looking like a bug. Null when a row moved and
    # there is nothing to explain.
    reason: str | None


# What the mail implies the board should say. Only kinds where the evidence is
# unambiguous about the OUTCOME - an acknowledgement means the application is
# alive, which the board already says, so it proposes nothing.
STATUS_FROM_EVENT = {
    "rejection": "Rejected",
    "position_closed": "No Longer Available",
    "offer": "Offer",
    "interview_invite": "Interviewing",
    "interview_scheduled": "Interviewing",
}

# Board statuses that mean "still waiting". A proposal is only worth making
# against one of these: if the user has already moved it on, the mail is
# confirming what they know rather than telling them something.
UNRESOLVED_BOARD_STATUSES = ("Application Submitted", "Follow-up")

# Why answering a proposal on an application with no board row moves nothing.
NOT_ON_BOARD = "This application is not on your board, so there is no status to move."

ACCEPTED = "accepted"
DISMISSED = "dismissed"


def proposals_for(user_id: int) -> list[Proposal]:
    """Where the mail and the board disagree, as things to confirm.

    Never an overwrite. `user_jobs.status` is what the user typed, and a system
    that silently rewrites it stops being trustworthy at exactly the moment it
    is most confident. So this says "we think you were rejected" and waits.

    Derived at read time rather than stored, so a proposal disappears on its
    own once the user acts on the board directly, or once a reclassification
    retracts the evidence. Only their ANSWER is a fact worth keeping.

    LEFT JOIN, not JOIN. The inner join asked "what does the board say" of
    applications that have no board row, which is 1,817 of 2,543 here: mail
    predating the catalog is the normal case and `applications.job_id` is
    nullable by design. It silenced 947 of 1,159 proposals - not by deciding
    they were wrong, but by never forming the question. The predicate that
    survives is the one that was actually meant: there is no board row to
    disagree with, or the board row still says the application is live.

    `board_updatable` says whether answering can move anything, so the caller
    states the consequence before the click rather than reporting a status it
    did not write afterwards.
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
    """Record the answer, and move the board only where there is a board.

    Returns None when the event is not currently evidence about this
    application, which the caller renders as a 404. The event has to belong to
    a message matched to THIS application - the same join the proposal list is
    built from. Checking only that the application is owned left `event_id`
    taken from the request and trusted, so another user's event could decide
    which status this application moved to.

    Both answers are recorded, against the EVENT, so a dismissal silences this
    piece of evidence rather than the question. A later rejection from the same
    company is new evidence and gets asked again - which is what makes
    dismissing safe rather than a decision that can never be revisited. A
    dismissal is also the signal that STATUS_FROM_EVENT is wrong for that case,
    which is only readable because the decline is stored rather than dropped.

    REPORTS WHAT IT TOUCHED, not what it would have touched. Accepting on an
    application with no board row wrote nothing and said the status had moved,
    so the caller was told about a change no SELECT could find.
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
        # The COUNT, not the fact that the statement ran. `applications.job_id`
        # survives the board row being deleted (ON DELETE SET NULL is on the
        # job, not on user_jobs), so a non-null job_id is not proof there is a
        # row to update - and an UPDATE that matched nothing exits cleanly.
        updated = db.execute_count(
            "UPDATE user_jobs SET status = %s, person_touched_at = now(), updated_at = now() "
            "WHERE user_id = %s AND job_id = %s",
            (status, user_id, app["job_id"]),
        )
    return ProposalAnswered(
        ok=True,
        response=response,
        board_updated=bool(updated),
        # Present only when a row actually moved. The proposed status is still
        # readable from `suggested_status` on the item that offered it.
        board_status=status if updated else None,
        # WHOLE SENTENCES, capitalised and stopped. These are shown to a person
        # verbatim - the client is told never to claim a move that did not
        # happen, so it renders this instead of composing its own line, and a
        # lowercase fragment lands mid-paragraph looking like a bug.
        reason=None
        if updated
        else (
            "Recorded. This application is not on your board, so no status moved."
            if response == ACCEPTED
            else "Recorded. Declining moves nothing, by design."
        ),
    )
