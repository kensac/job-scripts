"""One queue for everything awaiting a human decision, and one way to answer it.

Six resolve/undo pairs existed across six URL shapes with six payloads, each
individually well built. Together they were why the unmatched queue offered a
single verb - "say where this belongs" - when the honest answer is often
neither an application nor a correction.

The CHOICES ARE DECLARED BY THE SERVER. A row carries what it is, the evidence
behind it, and the verbs available on it, so the picker renders buttons from
data and a new decision type needs no frontend change. That is the part of
this that is architecture rather than plumbing.

Every answer APPENDS. Undo is another append, the wrong answer stays visible,
and `actor_user_id` records who decided - the same contract corrections got,
rather than a second one.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import db, mail_match, mail_pipeline
from api.auth import AuthedUser, require_user
from api.routers.admin import require_admin

router = APIRouter()

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

ASSIGN = "assign_application"
NOT_AN_APPLICATION = "not_an_application"
NOT_JOB_RELATED = "not_job_related"


class ResolveRequest(BaseModel):
    choice: Literal["assign_application", "not_an_application", "not_job_related"]
    target: int | None = None
    note: str | None = None


def _choice(
    choice: str,
    label: str,
    *,
    eligible: bool = True,
    reason: str | None = None,
    messages: int = 1,
) -> dict[str, Any]:
    """One verb, as the picker will render it.

    `reason` is PRINTED next to a refused verb rather than hidden behind a
    hover, so it has to be a short clause that survives being typeset inline.

    `affects` is omitted when the verb touches exactly one message, and
    omission MEANS one - never "unknown". A verb that can reach further and
    drops the field would wear a single-message costume, so a producer that
    cannot count precisely sends its best truth instead of nothing.
    """
    out: dict[str, Any] = {"choice": choice, "label": label, "eligible": eligible}
    if reason:
        out["reason"] = reason
    if messages > 1:
        out["affects"] = {"messages": messages}
    return out


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
LIMIT %(limit)s OFFSET %(offset)s
"""


def _thread_size(user_id: int, message_id: int, thread: str | None) -> int:
    """How many messages an assign would actually move.

    Assign carries the whole conversation by default, so the count belongs in
    the button rather than in the response afterwards - a person deciding one
    message should know before clicking that it moves fourteen.
    """
    if not thread:
        return 1
    row = db.query_one(
        "SELECT count(*) AS c FROM email_messages WHERE user_id = %s AND provider_thread_id = %s",
        (user_id, thread),
    )
    return max(1, int((row or {}).get("c", 1)))


def _queue_for(owner_id: int, limit: int, offset: int) -> dict[str, Any]:
    rows = db.query(
        _QUEUE_SQL,
        {
            "user": owner_id,
            "kinds": list(_AWAITING_KINDS),
            "refused": mail_match.NOT_AN_APPLICATION,
            "limit": limit,
            "offset": offset,
        },
    )
    if not rows:
        return {"items": [], "total": 0}

    apps = db.query(
        "SELECT id, company_name, title, applied_at FROM applications "
        "WHERE user_id = %s AND dismissed_at IS NULL",
        (owner_id,),
    )
    items = []
    for row in rows:
        key = mail_match.norm_company(row["company"])
        candidates = [a for a in apps if key and mail_match.norm_company(a["company_name"]) == key]
        moves = _thread_size(owner_id, row["id"], row["provider_thread_id"])
        if candidates:
            assign = _choice(ASSIGN, "Belongs to an application", messages=moves)
        else:
            # Not hidden. A verb that is unavailable and says why teaches more
            # than a verb that silently is not there.
            assign = _choice(
                ASSIGN,
                "Belongs to an application",
                eligible=False,
                reason="no application at this company yet",
            )
        items.append(
            {
                "id": f"message:{row['id']}",
                "kind": "unmatched_message",
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
                "choices": [
                    assign,
                    _choice(NOT_AN_APPLICATION, "Belongs to no application"),
                    _choice(NOT_JOB_RELATED, "Not job mail"),
                ],
            }
        )
    return {"items": items, "total": len(items)}


def _resolve(item_id: str, body: ResolveRequest, owner_id: int, actor_user_id: int) -> dict:
    kind, _, raw = item_id.partition(":")
    if kind != "message" or not raw.isdigit():
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
    message_id = int(raw)
    message = db.query_one(
        "SELECT id, user_id FROM email_messages WHERE id = %s AND user_id = %s",
        (message_id, owner_id),
    )
    if message is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})

    if body.choice == ASSIGN:
        if body.target is None:
            raise HTTPException(
                400, detail={"code": "TARGET_REQUIRED", "message": "assign needs an application"}
            )
        owner = db.query_one(
            "SELECT id FROM applications WHERE id = %s AND user_id = %s", (body.target, owner_id)
        )
        if owner is None:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown application"})
        mail_match.record(
            message_id,
            mail_match.Match(body.target, "manual", "high", body.note or "resolved by hand"),
            actor_user_id=actor_user_id,
        )
        mail_pipeline.sync_action_items(body.target)
        return {"ok": True, "choice": body.choice, "application_id": body.target}

    if body.choice == NOT_AN_APPLICATION:
        # Recorded as the matcher's own refusal, so every reader that already
        # tells a refusal from a failure sees it without learning a new value.
        mail_match.record(
            message_id,
            mail_match.Match(
                None, mail_match.NOT_AN_APPLICATION, "high", body.note or "refused by hand"
            ),
            actor_user_id=actor_user_id,
        )
        return {"ok": True, "choice": body.choice}

    # not_job_related: an append to the event log, the same retraction rule a
    # reclassification uses. The match is retracted too, because an event that
    # says this is not job mail cannot leave the message attached to a job.
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model, actor_user_id) "
        "VALUES (%s, 'not_job_related', 'high', %s, NULL, %s)",
        (message_id, db.jsonb({"corrected_by_user": True}), actor_user_id),
    )
    mail_match.record(
        message_id,
        mail_match.Match(None, mail_match.NOT_AN_APPLICATION, "high", "retracted: not job mail"),
        actor_user_id=actor_user_id,
    )
    return {"ok": True, "choice": body.choice}


@router.get("/user/resolve/queue")
def resolve_queue(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
):
    """Everything of the user's own awaiting a decision."""
    return _queue_for(user.id, limit, offset)


@router.post("/user/resolve/{item_id}")
def resolve_item(item_id: str, body: ResolveRequest, user: AuthedUser = Depends(require_user)):
    return _resolve(item_id, body, owner_id=user.id, actor_user_id=user.id)


@router.get("/admin/resolve/queue")
def admin_resolve_queue(
    user_id: int = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_admin),
):
    """The same queue over another user's mail. Owner is a parameter; the
    caller's identity decides only whether they may ask."""
    return _queue_for(user_id, limit, offset)


@router.post("/admin/resolve/{item_id}")
def admin_resolve_item(
    item_id: str,
    body: ResolveRequest,
    user_id: int = Query(...),
    user: AuthedUser = Depends(require_admin),
):
    return _resolve(item_id, body, owner_id=user_id, actor_user_id=user.id)
