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

# Job-related event kinds eligible for the queue (excludes not_job_related).
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


# Single-pass read of all queue-rankable rows via current_match_id /
# current_event_id pointers:
# 1. Messages currently matched to a live (non-dismissed) application.
# 2. Unmatched job-related messages that were not explicitly marked not_an_application.
# Keeps columns narrow for ranking; subject/sender/rationale are fetched for the
# page alone in queue_item_rendering. Ties break newest message_id first.
_CURRENT_SQL = """
WITH answered AS (
    SELECT DISTINCT event_id FROM event_answers WHERE question = 'status'
)
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
SELECT m.id, m.sent_at, e.id, e.kind, e.detail->>'company',
       NULL, NULL, NULL, NULL, false, false
FROM email_messages m
JOIN email_events e ON e.id = m.current_event_id
LEFT JOIN application_matches cm ON cm.id = m.current_match_id
WHERE m.user_id = %(user)s
  AND e.kind = ANY(%(kinds)s)
  AND cm.application_id IS NULL
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
    """Group current events by application ID (held equal to
    `mail_pipeline.events_by_application` by tests)."""
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
    """Unconfirmed automated attachments (`actor_user_id IS NULL` on the current match).

    Ranked at `RANK_MOVES_STAGE` when removing the message would change the
    application's derived stage, otherwise `RANK_ATTACHABLE`.
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
    """Newest unanswered status proposal per `(application_id, kind)` where mail
    disagrees with an unresolved or absent board row (held equal to
    `mail_pipeline.proposals_for` by tests)."""
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
    """Open asks ranked at `RANK_ATTACHABLE`, with `settles_on` naming which
    future event kinds would close each ask automatically."""
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
