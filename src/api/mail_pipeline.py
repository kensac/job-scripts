"""What an application's state IS, derived from the events about it.

Nothing here stores a status. State is a read-time function of the event log,
the same rule as job visibility, and for the same reason: every alternative
requires repair when something arrives late, out of order, or reclassified.

Because it is derived, all three of the awkward cases stop being special.
Applied before the posting reached the board, an email misfiled and corrected
later, a rejection that arrives before its own acknowledgement - each is just
a different set of events, recomputed.
"""

from __future__ import annotations

import logging
from typing import Any

from api import db

logger = logging.getLogger("jobtracker_worker")

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

# Events that create something the user has to do, and what it is called.
# recruiter_outreach is here deliberately even though it belongs to no
# application: an approach you never answer is still a decision you made, and
# it should be visible rather than lost because it had no job to attach to.
_EVENT_TO_ACTION = {
    "assessment_invite": "complete_assessment",
    "interview_invite": "schedule_interview",
    "info_request": "send_information",
    "offer": "respond_to_offer",
    "recruiter_outreach": "reply_to_recruiter",
}

# A later event that settles an earlier ask. This is what makes the system
# no-touch: an assessment invite is closed by the acknowledgement that follows
# it, not by the user remembering to tick something off.
_RESOLVING_EVENTS = {
    "complete_assessment": ("acknowledgement", "interview_invite", "rejection", "position_closed"),
    "schedule_interview": ("interview_scheduled", "rejection", "position_closed"),
    "send_information": ("acknowledgement", "interview_invite", "rejection", "position_closed"),
    "respond_to_offer": ("rejection",),
    "reply_to_recruiter": (),
}


def stage_for(events: list[dict[str, Any]]) -> str:
    """Furthest stage reached, with terminal events winning outright.

    Terminal beats progress regardless of order because a rejection is not
    undone by a later automated acknowledgement, and those do arrive - ATS
    systems send them on a schedule that has nothing to do with the decision.
    """
    terminal = [e for e in events if _EVENT_TO_STAGE.get(e["kind"]) in TERMINAL]
    if terminal:
        newest = max(terminal, key=lambda e: e["id"])
        return _EVENT_TO_STAGE[newest["kind"]]
    best = "applied"
    for event in events:
        stage = _EVENT_TO_STAGE.get(event["kind"])
        if stage in STAGE_ORDER and STAGE_ORDER.index(stage) > STAGE_ORDER.index(best):
            best = stage
    return best


def events_for(application_id: int) -> list[dict[str, Any]]:
    """Events reaching this application through its matched messages.

    DISTINCT ON (message_id), not (message_id, kind). A message is ONE thing -
    the classifier emits exactly one kind for it - so a correction from
    "rejection" to "interview_invite" has to RETRACT the rejection, and keying
    per kind would leave it live forever. Newest row per message wins.

    Matches are append-only for the same reason, so only the newest match per
    message counts as well: a message rematched to another application must
    stop contributing to the old one.
    """
    return db.query(
        """
        WITH current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id
            FROM application_matches ORDER BY message_id, id DESC
        ),
        current_event AS (
            SELECT DISTINCT ON (message_id) message_id, kind, id, occurred_at, deadline_at,
                   deadline_inferred
            FROM email_events ORDER BY message_id, id DESC
        )
        SELECT e.id, e.kind, e.occurred_at, e.deadline_at, e.deadline_inferred,
               m.sent_at, m.subject
        FROM current_match cm
        JOIN current_event e ON e.message_id = cm.message_id
        JOIN email_messages m ON m.id = cm.message_id
        WHERE cm.application_id = %s
        ORDER BY e.id
        """,
        (application_id,),
    )


def events_by_application(user_id: int) -> dict[int, list[dict[str, Any]]]:
    """Every application's events for one user, in one query.

    The per-application version is two queries each, which is fine for a
    detail view and ruinous for a list: 2,495 applications meant ~5,000
    queries to render one page. Same predicates as `events_for` - the two
    must agree, because a list and a detail disagreeing about an
    application's stage is worse than either being wrong alone.
    """
    rows = db.query(
        """
        WITH current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id
            FROM application_matches ORDER BY message_id, id DESC
        ),
        current_event AS (
            SELECT DISTINCT ON (message_id) message_id, kind, id, occurred_at, deadline_at,
                   deadline_inferred
            FROM email_events ORDER BY message_id, id DESC
        )
        SELECT cm.application_id, e.id, e.kind, e.occurred_at, e.deadline_at,
               e.deadline_inferred, m.sent_at, m.subject
        FROM current_match cm
        JOIN current_event e ON e.message_id = cm.message_id
        JOIN email_messages m ON m.id = cm.message_id
        JOIN applications a ON a.id = cm.application_id
        WHERE a.user_id = %s
        ORDER BY cm.application_id, e.id
        """,
        (user_id,),
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["application_id"], []).append(row)
    return grouped


def state_of(application_id: int) -> dict[str, Any]:
    events = events_for(application_id)
    return {
        "application_id": application_id,
        "stage": stage_for(events),
        "event_count": len(events),
        "last_event_at": max((e["sent_at"] for e in events if e["sent_at"]), default=None),
    }


def sync_action_items(application_id: int) -> dict[str, int]:
    """Open what the events ask for; close what later events have settled.

    Idempotent, because it runs on every recomputation. Opening is guarded by
    the event id so re-running cannot duplicate; closing is driven by a later
    event rather than by a timer, which is the difference between a system
    that maintains itself and a second inbox to maintain.
    """
    events = events_for(application_id)
    row = db.query_one("SELECT user_id FROM applications WHERE id = %s", (application_id,))
    if row is None:
        return {"opened": 0, "resolved": 0}
    user_id = row["user_id"]

    opened = 0
    for event in events:
        kind = _EVENT_TO_ACTION.get(event["kind"])
        if not kind:
            continue
        existing = db.query_one(
            "SELECT id FROM action_items WHERE event_id = %s AND kind = %s",
            (event["id"], kind),
        )
        if existing:
            continue
        db.execute(
            """
            INSERT INTO action_items (user_id, application_id, event_id, kind, due_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, application_id, event["id"], kind, event["deadline_at"]),
        )
        opened += 1

    resolved = 0
    open_items = db.query(
        "SELECT ai.id, ai.kind, ai.event_id FROM action_items ai "
        "WHERE ai.application_id = %s AND ai.resolved_at IS NULL",
        (application_id,),
    )
    # An item whose event no longer reaches this application is stranded: the
    # message was detached or rematched, so nothing will ever resolve it and it
    # stays open forever asking for something about an application it is not
    # part of. Matches are append-only, so the event did not disappear - it
    # moved, and the item has to follow.
    live_events = {e["id"] for e in events}
    for item in open_items:
        if item["event_id"] is not None and item["event_id"] not in live_events:
            db.execute(
                "UPDATE action_items SET resolved_at = now(), resolution = %s WHERE id = %s",
                ("the message this asked about is no longer part of this application", item["id"]),
            )
            resolved += 1
    open_items = [i for i in open_items if i["event_id"] in live_events or i["event_id"] is None]
    for item in open_items:
        settling = _RESOLVING_EVENTS.get(item["kind"], ())
        later = [e for e in events if e["kind"] in settling and e["id"] > (item["event_id"] or 0)]
        if not later:
            continue
        by = min(later, key=lambda e: e["id"])
        db.execute(
            "UPDATE action_items SET resolved_at = now(), resolution = %s, "
            "resolved_by_event_id = %s WHERE id = %s",
            (f"superseded by {by['kind']}", by["id"], item["id"]),
        )
        resolved += 1
    return {"opened": opened, "resolved": resolved}
