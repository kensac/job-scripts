"""Load and render the details for a ranked resolve queue page."""

from __future__ import annotations

from typing import Any

from api import db
from api.mail import pipeline as mail_pipeline
from api.resolve.choice_policy import _choice, choices_for_message
from api.resolve.contracts import (
    ACCEPT_STATUS,
    ACTION_ITEM,
    CANDIDATES,
    CONFIRM_MATCH,
    DECLINE_STATUS,
    REJECT_MATCH,
    STATUS_PROPOSAL,
    UNCONFIRMED_MATCH,
    UNMATCHED_MESSAGE,
)
from api.resolve.queue_types import QueueEvent, Ranked
from api.resolve.ranking import MESSAGE_RANK_REASONS, RANK_MOVES_STAGE

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
