from __future__ import annotations

from typing import Any

from api import db
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.resolve.choice_policy import _choice
from api.resolve.contracts import (
    ACTION_ITEM,
    MARK_DONE,
    STATUS_PROPOSAL,
    UNCONFIRMED_MATCH,
    UNMATCHED_MESSAGE,
)
from api.resolve.queue_item_rendering import build as build
from api.resolve.queue_types import QueueEvent, Ranked
from api.resolve.ranking import (
    RANK_ATTACHABLE,
    RANK_MOVES_STAGE,
    rank,
)

# Kinds that are about a job at all. `not_job_related` is excluded because it
# is already resolved - the classifier said this is not job mail and nothing
# is waiting on a person.
_AWAITING_KINDS = (
    "acknowledgement",
    "rejection",
    "interview_invite",
    "interview_scheduled",
    "assessment_invite",
    "info_request",
    "offer",
    "position_closed",
)


# EVERY ROW THE QUEUE RANKS, in one statement that reads the current event and
# the current match once. It used to be four statements, each recomputing
# the newest row per message over every email_events and application_matches
# row - 83,846 events four times per request, and again on the limit=1 re-read
# after every answer.
#
# Two disjoint populations, told apart by `application_id`:
#
#   - Messages whose current match is one of the owner's live applications.
#     These are the events behind every stage (what `events_for` reads), the
#     attachments nobody has confirmed, and the evidence proposals are made
#     from. Dismissed applications are left out: no row of the queue asks
#     about one, so their stage is never read.
#   - The owner's own mail with a job kind that reached no application and no
#     deliberate refusal.
#
# Narrow on purpose. Ranking needs kind, ids, board state and `sent_at`; the
# subject, sender, extracted title and match rationale are read for the page
# alone, by `_DETAIL_SQL`. Reading them for every row was most of the heap
# traffic into email_messages, whose rows carry the mail bodies.
#
# Ties on `sent_at` are ordered newest message first. Each of the four
# statements this replaces ordered by `sent_at` alone, so the order among
# equal timestamps was whatever the plan produced, not a property of the data.
_CURRENT_SQL = """
WITH answered AS (
    SELECT DISTINCT event_id FROM event_answers WHERE question = 'status'
)
-- Through the pointers, from rows already in hand: the owner's applications
-- reach their matches by index, and a match counts only while it is its
-- message's current one. No pass over every message or every event.
SELECT m.id AS message_id, m.sent_at, e.id AS event_id, e.kind, NULL AS company,
       cm.id AS match_id, cm.actor_user_id, a.id AS application_id,
       uj.status AS board_status, uj.user_id IS NOT NULL AS on_board,
       sr.event_id IS NOT NULL AS answered
FROM applications a
JOIN application_matches cm ON cm.application_id = a.id
JOIN email_messages m ON m.current_match_id = cm.id
LEFT JOIN email_events e ON e.id = m.current_event_id
LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
LEFT JOIN answered sr ON sr.event_id = e.id
WHERE a.user_id = %(user)s
  AND a.dismissed_at IS NULL
UNION ALL
-- What the matcher read the sender as is how an unmatched message finds its
-- candidates, so these rows carry it.
SELECT m.id, m.sent_at, e.id, e.kind, e.detail->>'company',
       NULL, NULL, NULL, NULL, false, false
FROM email_messages m
JOIN email_events e ON e.id = m.current_event_id
LEFT JOIN application_matches cm ON cm.id = m.current_match_id
WHERE m.user_id = %(user)s
  AND e.kind = ANY(%(kinds)s)
  AND cm.application_id IS NULL
  -- A deliberate refusal is already an answer. Only a failure to find one is
  -- still a question, and collapsing those two is the bug this queue exists
  -- to stop repeating.
  AND COALESCE(cm.method, '') <> %(refused)s
ORDER BY sent_at DESC NULLS LAST, message_id DESC
"""


def current_rows(owner_id: int) -> list[dict[str, Any]]:
    return db.query(
        _CURRENT_SQL,
        {
            "user": owner_id,
            "kinds": list(_AWAITING_KINDS),
            "refused": mail_match.NOT_AN_APPLICATION,
        },
    )


def events_by_application(rows: list[dict[str, Any]]) -> dict[int, list[QueueEvent]]:
    """`mail_pipeline.events_by_application` over the rows already read, for
    the owner's live applications.

    The same predicates - the current match is the application, the current
    event is the event - so the queue and the board agree about every stage.
    A test holds the two equal.
    """
    grouped: dict[int, list[QueueEvent]] = {}
    for row in rows:
        if row["application_id"] is not None and row["event_id"] is not None:
            grouped.setdefault(row["application_id"], []).append(
                QueueEvent(row["event_id"], row["kind"], row["message_id"])
            )
    for events in grouped.values():
        events.sort(key=lambda e: e.id)
    return grouped


def message_items(
    rows: list[dict[str, Any]],
    apps_by_company: dict[str, list[dict[str, Any]]],
    events: dict[int, list[QueueEvent]],
) -> list[Ranked]:
    """Mail that reached no application and no deliberate refusal."""
    out = []
    for row in rows:
        if row["application_id"] is not None:
            continue
        candidates = apps_by_company.get(mail_match.norm_company(row["company"]), [])
        out.append(
            Ranked(
                UNMATCHED_MESSAGE,
                rank(row["kind"], candidates, events),
                row["sent_at"],
                {**row, "candidates": candidates},
            )
        )
    return out


def match_items(rows: list[dict[str, Any]], events: dict[int, list[QueueEvent]]) -> list[Ranked]:
    """Attachments the matcher made that nobody has been asked about.

    `actor_user_id IS NULL` on the CURRENT row is the whole test, and it only
    became a true one when every human write started going through
    `mail_match.record`. Method cannot answer it: `manual` means a person chose
    the application, and 37 rows in production say `manual` with no actor
    because three endpoints wrote this table directly before that column
    existed.

    The stage a rejection would remove is the same question `rank` asks of an
    unmatched message, run backwards: an attachment holding an application at
    `rejected` is worth checking, because getting it wrong is the difference
    between a live application and a closed one.
    """
    stages: dict[int, str] = {}
    out = []
    for row in rows:
        app = row["application_id"]
        if app is None or row["actor_user_id"] is not None:
            continue
        own = events.get(app, [])
        if app not in stages:
            stages[app] = mail_pipeline.stage_for(own)
        # What this message contributes: the stage without it, against the
        # stage with everything. Equal means rejecting it changes nothing a
        # person would see.
        without = [e for e in own if e.message_id != row["message_id"]]
        moves = mail_pipeline.stage_for(without) != stages[app]
        out.append(
            Ranked(
                UNCONFIRMED_MATCH,
                RANK_MOVES_STAGE if moves else RANK_ATTACHABLE,
                row["sent_at"],
                {**row, "moves": moves, "stage": stages[app]},
            )
        )
    return out


def proposal_items(rows: list[dict[str, Any]]) -> list[Ranked]:
    """Where the mail and the board disagree, as `mail_pipeline.proposals_for`
    derives it, over the rows already read. A test holds the two equal.

    Never an overwrite: the board says the application is live, or there is no
    board row to disagree with, and the mail says otherwise. One per
    application and event kind, from the newest such event nobody has
    answered, in application then kind order.

    Every one of these moves what the product says, by construction, so they
    all rank at the top, and the reason says which way.
    """
    newest: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        if (
            row["application_id"] is None
            or row["kind"] not in mail_pipeline.STATUS_FROM_EVENT
            or row["answered"]
            or (
                row["on_board"]
                and row["board_status"] not in mail_pipeline.UNRESOLVED_BOARD_STATUSES
            )
        ):
            continue
        key = (row["application_id"], row["kind"])
        if key not in newest or row["event_id"] > newest[key]["event_id"]:
            newest[key] = row
    return [
        Ranked(STATUS_PROPOSAL, RANK_MOVES_STAGE, row["sent_at"], row)
        for _, row in sorted(newest.items())
    ]


def action_items(owner_id: int, events: dict[int, list[QueueEvent]]) -> list[Ranked]:
    """Open asks, each saying what could ever close it without a person.

    All at one rank, deliberately. Marking an ask done closes the ask and moves
    no stage, so the question this queue orders by - would answering change
    what the product says - has the same answer for every one of them.

    What differs is whether waiting is an option, and that is `settles_on`
    rather than a rank. It is not evenly true: `schedule_interview` is settled
    by a later event 117 times in 181, while `respond_to_offer` manages 8 in
    194, because the only event that closes an offer is a rejection and
    accepting one produces no mail at all. A rate is not a rank though, and
    cutting it somewhere would be a tuned number wearing a derivation's
    clothes. The list is the honest form: it says what would close this, and
    an empty one says nothing will.
    """
    open_apps = {
        r["id"]: r
        for r in db.query(
            """
            SELECT a.id, a.company_name, a.title, a.job_id, uj.status AS board_status,
                   uj.user_id IS NOT NULL AS on_board
            FROM applications a
            LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
            WHERE a.user_id = %s AND a.dismissed_at IS NULL
            """,
            (owner_id,),
        )
    }
    items = []
    for ask in mail_pipeline.action_items(owner_id, open_only=True):
        app = open_apps.get(ask.application_id)
        if app is None:
            continue
        message = {
            "id": ask.message_id,
            "subject": ask.subject,
            "from_email": ask.from_email,
            "sent_at": ask.sent_at,
        }
        item = {
            "id": f"action:{ask.id}",
            "kind": ACTION_ITEM,
            "rank": RANK_ATTACHABLE,
            "rank_reason": f"an incoming {' or '.join(ask.settles_on)} would close this"
            if ask.settles_on
            else "nothing that arrives can close this; only you can",
            "message": message,
            "application": {
                "id": app["id"],
                "company_name": app["company_name"],
                "title": app["title"],
                "stage": mail_pipeline.stage_for(events.get(app["id"], []), app["board_status"]),
                "on_board": bool(app["on_board"]),
                "job_id": app["job_id"],
            },
            "action": {
                "id": ask.id,
                "kind": ask.kind,
                "due_at": ask.due_at,
                "settles_on": ask.settles_on,
            },
            "choices": [_choice(MARK_DONE, "Done")],
        }
        items.append(Ranked(ACTION_ITEM, RANK_ATTACHABLE, ask.sent_at, item))
    return items
