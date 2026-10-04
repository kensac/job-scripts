"""The resolve queue as it was computed before it read the current event and
match once: four statements, every row built, then paged.

An oracle for tests/test_resolve_queue_equivalence.py, kept verbatim in what
it computes so the one-pass queue can be held to it. Its SQL orders unmatched
messages and unconfirmed matches by `sent_at` alone, so it is only an oracle
over data with no two rows of one of those kinds sharing a `sent_at`.

If the queue's response changes on purpose, change both or delete this.
"""

from __future__ import annotations

import datetime
from collections import Counter
from typing import Any

from api import db
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.resolve.choice_policy import _choice, _thread_sizes, by_company, choices_for_message
from api.resolve.contracts import (
    ACCEPT_STATUS,
    ACTION_ITEM,
    CANDIDATES,
    CONFIRM_MATCH,
    DECLINE_STATUS,
    ITEM_KINDS,
    MARK_DONE,
    REJECT_MATCH,
    STATUS_PROPOSAL,
    UNCONFIRMED_MATCH,
    UNMATCHED_MESSAGE,
)
from api.resolve.queue_items import _ACTIONS_SQL, _AWAITING_KINDS
from api.resolve.ranking import (
    MESSAGE_RANK_REASONS,
    RANK_ATTACHABLE,
    RANK_LABELS,
    RANK_MOVES_STAGE,
    rank,
)

_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)

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
  AND COALESCE(cm.method, '') <> %(refused)s
ORDER BY m.sent_at DESC NULLS LAST
"""

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


def _message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["message_id"] if "message_id" in row else row["id"],
        "subject": row["subject"],
        "from_email": row["from_email"],
        "sent_at": row["sent_at"],
        "classified_as": row["kind"],
        "extracted_company": row["company"],
        "extracted_title": row["role_title"],
    }


def _message_items(owner_id, apps_by_company, events):
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
                "rank_reason": MESSAGE_RANK_REASONS[item_rank],
                "message": _message(row),
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


def _match_items(owner_id, events):
    items = []
    for row in db.query(_UNCONFIRMED_SQL, {"user": owner_id}):
        own = events.get(row["application_id"], [])
        without = [e for e in own if e.message_id != row["message_id"]]
        moves = mail_pipeline.stage_for(without) != mail_pipeline.stage_for(own)
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
                "rank": RANK_MOVES_STAGE if moves else RANK_ATTACHABLE,
                "rank_reason": "this message is what puts the application where it is"
                if moves
                else "confirming or rejecting it would not move the stage",
                "message": _message(row),
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
                "implies": implies,
                "choices": [
                    _choice(CONFIRM_MATCH, "This is the right application"),
                    _choice(REJECT_MATCH, "This does not belong here"),
                ],
            }
        )
    return items


def _proposal_items(owner_id, events):
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


def _action_items(owner_id, events):
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


def queue_for(owner_id: int, limit: int, offset: int, kinds: list[str] | None = None):
    wanted = set(kinds or ITEM_KINDS)
    apps = db.query(
        "SELECT id, company_name, title, applied_at FROM applications "
        "WHERE user_id = %s AND dismissed_at IS NULL",
        (owner_id,),
    )
    events = mail_pipeline.events_by_application(owner_id)
    items: list[dict[str, Any]] = []
    if UNMATCHED_MESSAGE in wanted:
        items += _message_items(owner_id, by_company(apps), events)
    if UNCONFIRMED_MATCH in wanted:
        items += _match_items(owner_id, events)
    if STATUS_PROPOSAL in wanted:
        items += _proposal_items(owner_id, events)
    if ACTION_ITEM in wanted:
        items += _action_items(owner_id, events)
    items.sort(key=lambda i: (i.get("message") or {}).get("sent_at") or _EPOCH, reverse=True)
    items.sort(key=lambda i: i["rank"], reverse=True)
    by_rank = Counter(i["rank"] for i in items)
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "by_rank": {RANK_LABELS[k]: v for k, v in sorted(by_rank.items(), reverse=True)},
        "by_kind": dict(Counter(i["kind"] for i in items)),
    }
