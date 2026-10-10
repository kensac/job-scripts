from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, NamedTuple

from api import db
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.mail.current import current_event, current_match
from api.resolve.choice_policy import _choice, choices_for_message
from api.resolve.contracts import (
    ACCEPT_STATUS,
    ACTION_ITEM,
    CANDIDATES,
    CONFIRM_MATCH,
    DECLINE_STATUS,
    MARK_DONE,
    REJECT_MATCH,
    STATUS_PROPOSAL,
    UNCONFIRMED_MATCH,
    UNMATCHED_MESSAGE,
)
from api.resolve.ranking import (
    MESSAGE_RANK_REASONS,
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


class QueueEvent(NamedTuple):
    """An event as the queue reads it: what `stage_for` needs, and which
    message it came through so a match can be weighed without it."""

    id: int
    kind: str
    message_id: int


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
_CURRENT_SQL = f"""
WITH current_event AS (
    {current_event("id", "kind")}
),
current_match AS (
    {current_match("id", "application_id", "method", "actor_user_id")}
),
answered AS (
    SELECT DISTINCT application_id, event_id FROM suggestion_responses
)
SELECT cm.message_id, m.sent_at, e.id AS event_id, e.kind, NULL AS company,
       cm.id AS match_id, cm.actor_user_id, a.id AS application_id,
       uj.status AS board_status, uj.user_id IS NOT NULL AS on_board,
       sr.event_id IS NOT NULL AS answered
FROM current_match cm
JOIN applications a ON a.id = cm.application_id
JOIN email_messages m ON m.id = cm.message_id
LEFT JOIN current_event e ON e.message_id = cm.message_id
LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
LEFT JOIN answered sr ON sr.application_id = a.id AND sr.event_id = e.id
WHERE a.user_id = %(user)s
  AND a.dismissed_at IS NULL
UNION ALL
-- What the matcher read the sender as is how an unmatched message finds its
-- candidates, so these rows carry it.
SELECT m.id, m.sent_at, e.id, e.kind, ev.detail->>'company',
       NULL, NULL, NULL, NULL, false, false
FROM email_messages m
JOIN current_event e ON e.message_id = m.id
JOIN email_events ev ON ev.id = e.id
LEFT JOIN current_match cm ON cm.message_id = m.id
WHERE m.user_id = %(user)s
  AND e.kind = ANY(%(kinds)s)
  AND cm.application_id IS NULL
  -- A deliberate refusal is already an answer. Only a failure to find one is
  -- still a question, and collapsing those two is the bug this queue exists
  -- to stop repeating.
  AND COALESCE(cm.method, '') <> %(refused)s
ORDER BY sent_at DESC NULLS LAST, message_id DESC
"""

# The columns a row shows, for the rows on the page and no others. Positional
# rather than keyed by message, because one message can be on the page twice:
# as an attachment to confirm and as the evidence for a proposal.
#
# `thread_size` keeps `_thread_sizes`'s reading exactly, including its
# fallback: a message with no thread counts the owner's messages whose thread
# is the empty string, and one when there are none.
_DETAIL_SQL = """
SELECT p.n, m.subject, m.from_email,
       e.detail->>'company' AS company, e.detail->>'role_title' AS role_title,
       cm.method, cm.confidence, cm.rationale, cm.created_at,
       a.company_name, a.title, a.job_id,
       GREATEST(1, (SELECT count(*) FROM email_messages t
                    WHERE t.user_id = %(user)s
                      AND t.provider_thread_id = COALESCE(m.provider_thread_id, ''))) AS thread_size
FROM unnest(%(messages)s::bigint[], %(events)s::bigint[], %(matches)s::bigint[],
            %(applications)s::bigint[]) WITH ORDINALITY
     AS p(message_id, event_id, match_id, application_id, n)
JOIN email_messages m ON m.id = p.message_id
LEFT JOIN email_events e ON e.id = p.event_id
LEFT JOIN application_matches cm ON cm.id = p.match_id
LEFT JOIN applications a ON a.id = p.application_id
"""

_ACTIONS_SQL = """
SELECT ai.id, ai.kind, ai.due_at, ai.application_id, ai.event_id,
       a.company_name, a.title, a.job_id, uj.status AS board_status,
       uj.user_id IS NOT NULL AS on_board,
       m.id AS message_id, m.subject, m.from_email, m.sent_at
FROM action_items ai
LEFT JOIN applications a ON a.id = ai.application_id
LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
LEFT JOIN email_events e ON e.id = ai.event_id
LEFT JOIN email_messages m ON m.id = e.message_id
WHERE ai.user_id = %(user)s
  AND ai.resolved_at IS NULL
  AND (a.id IS NULL OR a.dismissed_at IS NULL)
ORDER BY ai.due_at NULLS LAST, ai.id
"""


@dataclass(frozen=True, slots=True)
class Ranked:
    """A queue row before it is built: enough to order it and count it.

    The queue ranks before it pages, so every row has to be ranked, but only
    the page is shown. `row` is the ranking row, or for an action item the
    finished item, since those are few and complete in one statement.
    """

    kind: str
    rank: int
    sent_at: datetime.datetime | None
    row: dict[str, Any]


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
    items = []
    for row in db.query(_ACTIONS_SQL, {"user": owner_id}):
        settling = mail_pipeline.settles_on(row["kind"])
        message = (
            {
                "id": row["message_id"],
                "subject": row["subject"],
                "from_email": row["from_email"],
                "sent_at": row["sent_at"],
            }
            if row["message_id"]
            else None
        )
        item = {
            "id": f"action:{row['id']}",
            "kind": ACTION_ITEM,
            "rank": RANK_ATTACHABLE,
            "rank_reason": f"an incoming {' or '.join(settling)} would close this"
            if settling
            else "nothing that arrives can close this; only you can",
            "message": message,
            "application": {
                "id": row["application_id"],
                "company_name": row["company_name"],
                "title": row["title"],
                "stage": mail_pipeline.stage_for(
                    events.get(row["application_id"], []), row["board_status"]
                ),
                "on_board": bool(row["on_board"]),
                "job_id": row["job_id"],
            }
            if row["application_id"]
            else None,
            "action": {
                "id": row["id"],
                "kind": row["kind"],
                "due_at": row["due_at"],
                "settles_on": settling,
            },
            "choices": [_choice(MARK_DONE, "Done")],
        }
        items.append(
            Ranked(ACTION_ITEM, RANK_ATTACHABLE, message["sent_at"] if message else None, item)
        )
    return items


def build(
    owner_id: int,
    page: list[Ranked],
    apps_by_company: dict[str, list[dict[str, Any]]],
    events: dict[int, list[QueueEvent]],
) -> list[dict[str, Any]]:
    """The page's rows as the queue shows them, reading their columns once."""
    asked = [r.row for r in page if r.kind != ACTION_ITEM]
    details: dict[int, dict[str, Any]] = {}
    if asked:
        for d in db.query(
            _DETAIL_SQL,
            {
                "user": owner_id,
                "messages": [r["message_id"] for r in asked],
                "events": [r["event_id"] for r in asked],
                "matches": [r["match_id"] for r in asked],
                "applications": [r["application_id"] for r in asked],
            },
        ):
            details[d["n"]] = d
    out = []
    n = 0
    for ranked in page:
        if ranked.kind == ACTION_ITEM:
            out.append(ranked.row)
            continue
        n += 1
        row, detail = ranked.row, details[n]
        message = {
            "id": row["message_id"],
            "subject": detail["subject"],
            "from_email": detail["from_email"],
            "sent_at": row["sent_at"],
            "classified_as": row["kind"],
            "extracted_company": detail["company"],
            "extracted_title": detail["role_title"],
        }
        if ranked.kind == UNMATCHED_MESSAGE:
            out.append(_message_item(ranked, message, detail, apps_by_company))
        elif ranked.kind == UNCONFIRMED_MATCH:
            out.append(_match_item(ranked, message, detail))
        else:
            out.append(_proposal_item(ranked, message, detail, events))
    return out


def _message_item(
    ranked: Ranked,
    message: dict[str, Any],
    detail: dict[str, Any],
    apps_by_company: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "id": f"message:{ranked.row['message_id']}",
        "kind": UNMATCHED_MESSAGE,
        "rank": ranked.rank,
        # Why it sits where it does, so the ordering is answerable rather
        # than something the page has to take on trust.
        "rank_reason": MESSAGE_RANK_REASONS[ranked.rank],
        "message": message,
        # The matcher refused to choose between these on purpose, which is
        # exactly the decision a person is best placed to settle.
        "candidates": ranked.row["candidates"],
        "choices": choices_for_message(
            apps_by_company, detail["company"], detail["thread_size"], CANDIDATES
        ),
    }


def _match_item(ranked: Ranked, message: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    row = ranked.row
    implies = None
    if row["kind"] in mail_pipeline.STATUS_FROM_EVENT:
        implies = {
            "board_status": mail_pipeline.STATUS_FROM_EVENT[row["kind"]],
            "board_updated": bool(row["on_board"]),
            "reason": None
            if row["on_board"]
            else "This application is not on your board, so no status would move.",
        }
    return {
        "id": f"match:{row['match_id']}",
        "kind": UNCONFIRMED_MATCH,
        "rank": ranked.rank,
        "rank_reason": "this message is what puts the application where it is"
        if row["moves"]
        else "confirming or rejecting it would not move the stage",
        "message": message,
        "application": {
            "id": row["application_id"],
            "company_name": detail["company_name"],
            "title": detail["title"],
            "stage": row["stage"],
            "on_board": bool(row["on_board"]),
            "job_id": detail["job_id"],
        },
        "match": {
            "id": row["match_id"],
            "method": detail["method"],
            "confidence": detail["confidence"],
            "rationale": detail["rationale"],
            "created_at": detail["created_at"],
        },
        # Declared before the click, because confirming can carry a board
        # change with it and a person should know that first.
        "implies": implies,
        "choices": [
            _choice(CONFIRM_MATCH, "This is the right application"),
            _choice(REJECT_MATCH, "This does not belong here"),
        ],
    }


def _proposal_item(
    ranked: Ranked,
    message: dict[str, Any],
    detail: dict[str, Any],
    events: dict[int, list[QueueEvent]],
) -> dict[str, Any]:
    """The row carries the SAME message block the other kinds carry - subject,
    sender, what it was read as - and the stage the application's mail puts
    it at. It shipped with a bare message id and a null stage, so a person
    deciding whether an application was rejected saw the company name, the
    proposed status, and nothing to check either against. Measured
    2026-09-04: 664 of 1,159 waiting proposals sit on an application with
    two or more matched messages, and none of that history was reachable
    from the row.
    """
    row = ranked.row
    suggested = mail_pipeline.STATUS_FROM_EVENT[row["kind"]]
    return {
        "id": f"proposal:{row['application_id']}:{row['event_id']}",
        "kind": STATUS_PROPOSAL,
        "rank": RANK_MOVES_STAGE,
        "rank_reason": "the mail and your board disagree about this application",
        "message": message,
        "application": {
            "id": row["application_id"],
            "company_name": detail["company_name"],
            "title": detail["title"],
            "stage": mail_pipeline.stage_for(
                events.get(row["application_id"], []), row["board_status"]
            ),
            "on_board": bool(row["on_board"]),
            "job_id": detail["job_id"],
        },
        "implies": {
            "board_status": suggested,
            "from_status": row["board_status"],
            "board_updated": bool(row["on_board"]),
            "reason": None if row["on_board"] else mail_pipeline.NOT_ON_BOARD,
        },
        "choices": [
            _choice(ACCEPT_STATUS, f"Move it to {suggested}"),
            _choice(DECLINE_STATUS, "Leave it where it is"),
        ],
    }
