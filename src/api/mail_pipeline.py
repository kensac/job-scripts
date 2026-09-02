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
from core import ats

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


# Board statuses that mean the user withdrew. `withdrawn` is terminal and has
# no event that produces it, because no employer sends mail saying you pulled
# out - it is the one stage only the person can assert. Without this it was
# declared vocabulary with no producer: a state the API named, the frontend
# rendered a column for, and nothing could ever reach.
WITHDRAWN_STATUSES = ("No Longer Interested", "Withdrawn")


def stage_for(events: list[dict[str, Any]], board_status: str | None = None) -> str:
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
    row = db.query_one(
        "SELECT uj.status FROM applications a "
        "LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id "
        "WHERE a.id = %s",
        (application_id,),
    )
    return {
        "application_id": application_id,
        "stage": stage_for(events, (row or {}).get("status")),
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


# A non-ATS sender domain that has produced this many DISTINCT company names is
# an intermediary rather than one employer's own mail server. Derived, not
# picked: bucketing every mail-derived application by its sender's spread puts
# the junk rate at 4% for domains naming one company and 9% for two - the base
# rate - then 41% at three to four and 32% at five or more. The jump sits
# between two and three.
INTERMEDIARY_COMPANY_NAMES = 3


def sender_signal(user_id: int) -> dict[int, dict[str, Any]]:
    """Per application, what its FIRST message's sender says about whether this
    is an employer relationship at all.

    Computed at read time and deliberately not stored. The ATS half is a
    property of the sender domain, so it has to move when core.ats learns a new
    provider; the spread half is a function of the user's whole message
    history, so a domain that becomes an intermediary after a match was made
    has to change the answer for matches already written. Freezing either at
    match time would preserve the wrong one.

    A POSSIBILITY, NEVER A VERDICT. Nothing here drops or hides an application.
    The strongest thing it says is "worth a look", because the failure this
    exists to catch - an organisation the user has a relationship with that is
    not an employer he applied to - is also the shape of a perfectly real
    application through a job board.

    What it does NOT do is guess from the company name. Blocking universities
    breaks applying to a university for a job, which is normal; the three
    organisations that motivated this were caught by three different special
    cases, and a fourth would have needed a fifth.
    """
    rows = db.query(
        """
        WITH first_message AS (
            SELECT DISTINCT ON (am.application_id) am.application_id, m.from_email
            FROM application_matches am
            JOIN email_messages m ON m.id = am.message_id
            JOIN applications a ON a.id = am.application_id
            WHERE am.application_id IS NOT NULL AND a.user_id = %(user_id)s
            ORDER BY am.application_id, m.sent_at ASC
        ),
        with_domain AS (
            SELECT f.application_id,
                   lower(COALESCE(NULLIF(split_part(f.from_email, '@', 2), ''), '')) AS domain,
                   COALESCE(a.company_name, '') AS company
            FROM first_message f
            JOIN applications a ON a.id = f.application_id
        ),
        spread AS (
            SELECT domain, count(DISTINCT company) AS companies
            FROM with_domain WHERE domain <> '' GROUP BY domain
        )
        SELECT w.application_id, w.domain, COALESCE(s.companies, 0) AS companies
        FROM with_domain w LEFT JOIN spread s ON s.domain = w.domain
        """,
        {"user_id": user_id},
    )
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        domain = row["domain"] or None
        companies = int(row["companies"] or 0)
        is_ats = ats.is_ats_email_domain(domain)
        shared = bool(domain) and not is_ats and companies >= INTERMEDIARY_COMPANY_NAMES
        if is_ats:
            why = "sent by an applicant-tracking system, which is near-proof of a real application"
        elif shared:
            why = (
                f"{domain} is the first sender for {companies} different companies, so it is a "
                "platform or an organisation you have a standing relationship with rather than "
                "one employer's own mail - worth confirming you applied here"
            )
        else:
            why = "sent from the employer's own domain"
        out[row["application_id"]] = {
            "sender_domain": domain,
            "sender_is_ats": is_ats,
            # How many distinct companies this user has derived from that
            # sender. Exposed rather than reduced to a flag, because it is the
            # evidence and the flag is only a reading of it.
            "sender_company_count": companies,
            "review_suggested": shared,
            "why": why,
        }
    return out


def intermediary_domains() -> list[str]:
    """Sender domains that are platforms rather than one employer's own mail.

    The same rule as sender_signal, across every user rather than one, for the
    aggregate surfaces that have no user in scope. Defined once here so the
    threshold and the ATS exemption cannot drift between the per-application
    reading and the per-company one.
    """
    rows = db.query(
        """
        WITH first_message AS (
            SELECT DISTINCT ON (am.application_id) am.application_id, m.from_email
            FROM application_matches am
            JOIN email_messages m ON m.id = am.message_id
            WHERE am.application_id IS NOT NULL
            ORDER BY am.application_id, m.sent_at ASC
        )
        SELECT lower(COALESCE(NULLIF(split_part(f.from_email, '@', 2), ''), '')) AS domain,
               count(DISTINCT COALESCE(a.company_name, '')) AS companies
        FROM first_message f
        JOIN applications a ON a.id = f.application_id
        WHERE a.dismissed_at IS NULL
        GROUP BY 1
        """
    )
    return [
        r["domain"]
        for r in rows
        if r["domain"]
        and not ats.is_ats_email_domain(r["domain"])
        and int(r["companies"]) >= INTERMEDIARY_COMPANY_NAMES
    ]
