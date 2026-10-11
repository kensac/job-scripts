"""Current mail events and derived application stages."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from api import db
from api.mail.current import current_event, current_match


class ApplicationEvent(BaseModel):
    """One event reaching an application, through the message it was read from.

    The row `events_for` and `events_by_application` both select, declared
    once because a list and a detail view disagreeing about an application's
    events is worse than either being wrong alone. `stage_for` reads `kind`
    and `id`; everything else is what a reader needs to check the stage
    against the mail that produced it.
    """

    id: int
    kind: str
    occurred_at: datetime.datetime | None
    deadline_at: datetime.datetime | None
    deadline_inferred: bool
    message_id: int
    sent_at: datetime.datetime | None
    subject: str | None

    @classmethod
    def hypothetical(cls, kind: str, after: int) -> ApplicationEvent:
        """An event that has NOT arrived, for asking what one would do.

        `after` has to be newer than every real event: `stage_for` breaks ties
        among terminal events by taking the newest, so a rejection already
        present would otherwise win over the one being considered and the
        answer would be "changes nothing" for the case that changes the most.

        There is no message, because there is no event - message_id is 0 and
        this never leaves the caller that built it.
        """
        return cls(
            id=after,
            kind=kind,
            occurred_at=None,
            deadline_at=None,
            deadline_inferred=False,
            message_id=0,
            sent_at=None,
            subject=None,
        )


class Staged(Protocol):
    """What `stage_for` reads of an event, and nothing else.

    The queue ranks thousands of rows by stage and needs only these two
    fields, so it reads them without the message columns `ApplicationEvent`
    carries for a person checking the stage.
    """

    @property
    def id(self) -> int: ...

    @property
    def kind(self) -> str: ...


class ApplicationState(BaseModel):
    """What the events say about one application, and how much they say it
    from. `last_event_at` is null when nothing has arrived, which is not the
    same as nothing having been looked at."""

    application_id: int
    stage: str
    event_count: int
    last_event_at: datetime.datetime | None


# Ordered by how far through a process they are. A later stage never regresses
# to an earlier one on the strength of an older email: an acknowledgement that
# arrives after a rejection is mail delivery being untidy, not the employer
# changing their mind.
STAGE_ORDER = (
    "applied",
    "acknowledged",
    "assessment",
    "interviewing",
    "offer",
)

TERMINAL = ("rejected", "withdrawn", "closed")

_EVENT_TO_STAGE = {
    "acknowledgement": "acknowledged",
    "assessment_invite": "assessment",
    "interview_invite": "interviewing",
    "interview_scheduled": "interviewing",
    "info_request": "interviewing",
    "offer": "offer",
    "rejection": "rejected",
    "position_closed": "closed",
}

# Board statuses that mean the user withdrew. `withdrawn` is terminal and has
# no event that produces it, because no employer sends mail saying you pulled
# out - it is the one stage only the person can assert. Without this it was
# declared vocabulary with no producer: a state the API named, the frontend
# rendered a column for, and nothing could ever reach.
WITHDRAWN_STATUSES = ("No Longer Interested", "Withdrawn")


def stage_for(events: Sequence[Staged], board_status: str | None = None) -> str:
    """Furthest stage reached, with terminal events winning outright.

    Terminal beats progress regardless of order because a rejection is not
    undone by a later automated acknowledgement, and those do arrive - ATS
    systems send them on a schedule that has nothing to do with the decision.
    """
    # Withdrawal beats everything, including a later automated acknowledgement,
    # because it is the only stage the person asserts directly rather than
    # something inferred from what an employer sent.
    if board_status in WITHDRAWN_STATUSES:
        return "withdrawn"
    terminal = [e for e in events if _EVENT_TO_STAGE.get(e.kind) in TERMINAL]
    if terminal:
        newest = max(terminal, key=lambda e: e.id)
        return _EVENT_TO_STAGE[newest.kind]
    best = "applied"
    for event in events:
        stage = _EVENT_TO_STAGE.get(event.kind)
        if stage in STAGE_ORDER and STAGE_ORDER.index(stage) > STAGE_ORDER.index(best):
            best = stage
    return best


def events_for(application_id: int) -> list[ApplicationEvent]:
    """Events reaching this application through its matched messages.

    Newest event per message, not per (message, kind). A message is ONE thing -
    the classifier emits exactly one kind for it - so a correction from
    "rejection" to "interview_invite" has to RETRACT the rejection, and keying
    per kind would leave it live forever. Newest row per message wins.

    Matches are append-only for the same reason, so only the newest match per
    message counts as well: a message rematched to another application must
    stop contributing to the old one.
    """
    return db.query_as(
        ApplicationEvent,
        f"""
        WITH current_match AS (
            {current_match("application_id")}
        ),
        current_event AS (
            {current_event("kind", "id", "occurred_at", "deadline_at", "deadline_inferred")}
        )
        SELECT e.id, e.kind, e.occurred_at, e.deadline_at, e.deadline_inferred,
               m.id AS message_id, m.sent_at, m.subject
        FROM current_match cm
        JOIN current_event e ON e.message_id = cm.message_id
        JOIN email_messages m ON m.id = cm.message_id
        WHERE cm.application_id = %s
        ORDER BY e.id
        """,
        (application_id,),
    )


def events_by_application(user_id: int) -> dict[int, list[ApplicationEvent]]:
    """Every application's events for one user, in one query.

    The per-application version is two queries each, which is fine for a
    detail view and ruinous for a list: 2,495 applications meant ~5,000
    queries to render one page. Same predicates as `events_for` - the two
    must agree, because a list and a detail disagreeing about an
    application's stage is worse than either being wrong alone.

    The application is the KEY rather than a field, so the events are the
    same shape `events_for` returns and a caller cannot be handed one kind
    here and another kind there.
    """
    rows = db.query(
        f"""
        WITH current_match AS (
            {current_match("application_id")}
        ),
        current_event AS (
            {current_event("kind", "id", "occurred_at", "deadline_at", "deadline_inferred")}
        )
        SELECT cm.application_id, e.id, e.kind, e.occurred_at, e.deadline_at,
               e.deadline_inferred, m.id AS message_id, m.sent_at, m.subject
        FROM current_match cm
        JOIN current_event e ON e.message_id = cm.message_id
        JOIN email_messages m ON m.id = cm.message_id
        JOIN applications a ON a.id = cm.application_id
        WHERE a.user_id = %s
        ORDER BY cm.application_id, e.id
        """,
        (user_id,),
    )
    grouped: dict[int, list[ApplicationEvent]] = {}
    for row in rows:
        grouped.setdefault(row.pop("application_id"), []).append(ApplicationEvent(**row))
    return grouped


def state_of(application_id: int) -> ApplicationState:
    events = events_for(application_id)
    row = db.query_one(
        "SELECT uj.status FROM applications a "
        "LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id "
        "WHERE a.id = %s",
        (application_id,),
    )
    return ApplicationState(
        application_id=application_id,
        stage=stage_for(events, (row or {}).get("status")),
        event_count=len(events),
        last_event_at=max((e.sent_at for e in events if e.sent_at), default=None),
    )
