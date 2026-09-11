from __future__ import annotations

from typing import Any

from api import db
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.resolve.choice_policy import _choice, _thread_sizes, choices_for_message
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

_QUEUE_SQL = """
WITH current_event AS (
    SELECT DISTINCT ON (message_id) message_id, kind, detail
    FROM email_events ORDER BY message_id, id DESC
),
current_match AS (
    SELECT DISTINCT ON (message_id) message_id, application_id, method
    FROM application_matches ORDER BY message_id, id DESC
)
SELECT m.id, m.subject, m.from_email, m.sent_at, m.provider_thread_id,
       e.kind, e.detail->>'company' AS company, e.detail->>'role_title' AS role_title
FROM email_messages m
JOIN current_event e ON e.message_id = m.id
LEFT JOIN current_match cm ON cm.message_id = m.id
WHERE m.user_id = %(user)s
  AND e.kind = ANY(%(kinds)s)
  AND cm.application_id IS NULL
  -- A deliberate refusal is already an answer. Only a failure to find one is
  -- still a question, and collapsing those two is the bug this queue exists
  -- to stop repeating.
  AND COALESCE(cm.method, '') <> %(refused)s
ORDER BY m.sent_at DESC NULLS LAST
"""


# Attachments standing right now that no person has ever looked at.
#
# `actor_user_id IS NULL` on the CURRENT row is the whole test, and it only
# became a true one when every human write started going through
# `mail_match.record`. Method cannot answer it: `manual` means a person chose
# the application, and 37 rows in production say `manual` with no actor because
# three endpoints wrote this table directly before that column existed.
_UNCONFIRMED_SQL = """
WITH current_match AS (
    SELECT DISTINCT ON (message_id) message_id, id, application_id, method, confidence,
           rationale, actor_user_id, created_at
    FROM application_matches ORDER BY message_id, id DESC
),
current_event AS (
    SELECT DISTINCT ON (message_id) message_id, kind, detail
    FROM email_events ORDER BY message_id, id DESC
)
SELECT cm.id AS match_id, cm.application_id, cm.method, cm.confidence, cm.rationale,
       cm.created_at, m.id AS message_id, m.subject, m.from_email, m.sent_at,
       e.kind, e.detail->>'company' AS company, e.detail->>'role_title' AS role_title,
       a.company_name, a.title, a.job_id, uj.user_id IS NOT NULL AS on_board
FROM current_match cm
JOIN email_messages m ON m.id = cm.message_id
JOIN applications a ON a.id = cm.application_id
LEFT JOIN current_event e ON e.message_id = m.id
LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
WHERE a.user_id = %(user)s
  AND a.dismissed_at IS NULL
  AND cm.application_id IS NOT NULL
  AND cm.actor_user_id IS NULL
ORDER BY m.sent_at DESC NULLS LAST
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


def message_items(
    owner_id: int,
    apps_by_company: dict[str, list[dict[str, Any]]],
    events: dict[int, list[mail_pipeline.ApplicationEvent]],
) -> list[dict[str, Any]]:
    """Mail that reached no application and no deliberate refusal."""
    rows = db.query(
        _QUEUE_SQL,
        {
            "user": owner_id,
            "kinds": list(_AWAITING_KINDS),
            "refused": mail_match.NOT_AN_APPLICATION,
        },
    )
    if not rows:
        return []
    threads = _thread_sizes(owner_id)
    items = []
    for row in rows:
        candidates = apps_by_company.get(mail_match.norm_company(row["company"]), [])
        item_rank = rank(row["kind"], candidates, events)
        items.append(
            {
                "id": f"message:{row['id']}",
                "kind": UNMATCHED_MESSAGE,
                "rank": item_rank,
                # Why it sits where it does, so the ordering is answerable
                # rather than something the page has to take on trust.
                "rank_reason": MESSAGE_RANK_REASONS[item_rank],
                "message": {
                    "id": row["id"],
                    "subject": row["subject"],
                    "from_email": row["from_email"],
                    "sent_at": row["sent_at"],
                    "classified_as": row["kind"],
                    "extracted_company": row["company"],
                    "extracted_title": row["role_title"],
                },
                # The matcher refused to choose between these on purpose, which
                # is exactly the decision a person is best placed to settle.
                "candidates": candidates,
                "choices": choices_for_message(
                    apps_by_company,
                    row["company"],
                    threads.get(row["provider_thread_id"] or "", 1),
                    CANDIDATES,
                ),
            }
        )
    return items


def match_items(
    owner_id: int, events: dict[int, list[mail_pipeline.ApplicationEvent]]
) -> list[dict[str, Any]]:
    """Attachments the matcher made that nobody has been asked about.

    The stage a rejection would remove is the same question `rank` asks of an
    unmatched message, run backwards: an attachment holding an application at
    `rejected` is worth checking, because getting it wrong is the difference
    between a live application and a closed one.
    """
    rows = db.query(_UNCONFIRMED_SQL, {"user": owner_id})
    items = []
    for row in rows:
        own = events.get(row["application_id"], [])
        # What this message contributes: the stage without it, against the
        # stage with everything. Equal means rejecting it changes nothing a
        # person would see.
        without = [e for e in own if e.message_id != row["message_id"]]
        moves = mail_pipeline.stage_for(without) != mail_pipeline.stage_for(own)
        item_rank = RANK_MOVES_STAGE if moves else RANK_ATTACHABLE
        implies = None
        if row["kind"] in mail_pipeline.STATUS_FROM_EVENT:
            implies = {
                "board_status": mail_pipeline.STATUS_FROM_EVENT[row["kind"]],
                "board_updated": bool(row["on_board"]),
                "reason": None
                if row["on_board"]
                else "This application is not on your board, so no status would move.",
            }
        items.append(
            {
                "id": f"match:{row['match_id']}",
                "kind": UNCONFIRMED_MATCH,
                "rank": item_rank,
                "rank_reason": "this message is what puts the application where it is"
                if moves
                else "confirming or rejecting it would not move the stage",
                "message": {
                    "id": row["message_id"],
                    "subject": row["subject"],
                    "from_email": row["from_email"],
                    "sent_at": row["sent_at"],
                    "classified_as": row["kind"],
                    "extracted_company": row["company"],
                    "extracted_title": row["role_title"],
                },
                "application": {
                    "id": row["application_id"],
                    "company_name": row["company_name"],
                    "title": row["title"],
                    "stage": mail_pipeline.stage_for(own),
                    "on_board": bool(row["on_board"]),
                    "job_id": row["job_id"],
                },
                "match": {
                    "id": row["match_id"],
                    "method": row["method"],
                    "confidence": row["confidence"],
                    "rationale": row["rationale"],
                    "created_at": row["created_at"],
                },
                # Declared before the click, because confirming can carry a
                # board change with it and a person should know that first.
                "implies": implies,
                "choices": [
                    _choice(CONFIRM_MATCH, "This is the right application"),
                    _choice(REJECT_MATCH, "This does not belong here"),
                ],
            }
        )
    return items


def proposal_items(
    owner_id: int, events: dict[int, list[mail_pipeline.ApplicationEvent]]
) -> list[dict[str, Any]]:
    """Where the mail and the board disagree.

    Every one of these moves what the product says, by construction - a
    proposal is only made where the board still says the application is live
    and the mail says it is not. So they all rank at the top, and the reason
    says which way.

    The row carries the SAME message block the other kinds carry - subject,
    sender, what it was read as - and the stage the application's mail puts
    it at. It shipped with a bare message id and a null stage, so a person
    deciding whether an application was rejected saw the company name, the
    proposed status, and nothing to check either against. Measured
    2026-09-04: 664 of 1,159 waiting proposals sit on an application with
    two or more matched messages, and none of that history was reachable
    from the row.
    """
    items = []
    for row in mail_pipeline.proposals_for(owner_id):
        items.append(
            {
                "id": f"proposal:{row.application_id}:{row.event_id}",
                "kind": STATUS_PROPOSAL,
                "rank": RANK_MOVES_STAGE,
                "rank_reason": "the mail and your board disagree about this application",
                "message": {
                    "id": row.message_id,
                    "subject": row.subject,
                    "from_email": row.from_email,
                    "sent_at": row.sent_at,
                    "classified_as": row.kind,
                    "extracted_company": row.company,
                    "extracted_title": row.role_title,
                },
                "application": {
                    "id": row.application_id,
                    "company_name": row.company_name,
                    "title": row.title,
                    "stage": mail_pipeline.stage_for(
                        events.get(row.application_id, []), row.board_status
                    ),
                    "on_board": bool(row.board_updatable),
                    "job_id": row.job_id,
                },
                "implies": {
                    "board_status": row.suggested_status,
                    "from_status": row.board_status,
                    "board_updated": bool(row.board_updatable),
                    "reason": row.board_reason,
                },
                "choices": [
                    _choice(ACCEPT_STATUS, f"Move it to {row.suggested_status}"),
                    _choice(DECLINE_STATUS, "Leave it where it is"),
                ],
            }
        )
    return items


def action_items(
    owner_id: int, events: dict[int, list[mail_pipeline.ApplicationEvent]]
) -> list[dict[str, Any]]:
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
        items.append(
            {
                "id": f"action:{row['id']}",
                "kind": ACTION_ITEM,
                "rank": RANK_ATTACHABLE,
                "rank_reason": f"an incoming {' or '.join(settling)} would close this"
                if settling
                else "nothing that arrives can close this; only you can",
                "message": {
                    "id": row["message_id"],
                    "subject": row["subject"],
                    "from_email": row["from_email"],
                    "sent_at": row["sent_at"],
                }
                if row["message_id"]
                else None,
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
        )
    return items
