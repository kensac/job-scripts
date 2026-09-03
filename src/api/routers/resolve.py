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

import datetime
from collections import Counter
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import db, mail_match, mail_pipeline
from api.auth import AuthedUser, require_user
from api.routers.admin import require_admin

router = APIRouter()

_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)

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


# DECLARED RESPONSE SCHEMAS, which is not the house style yet and should be.
# 123 of the 128 operations in openapi.json ship `"schema": {}`, so a client
# and this server can disagree about an envelope and nothing mechanical
# notices - four such mismatches were found by hand in one day, every one
# silent. A generated schema is the only place that drift becomes detectable,
# and the newest surface is the cheapest place to start rather than a
# retrofit.
class ResolveChoiceAffects(BaseModel):
    messages: int


# exclude_none on the routes below is load-bearing, not tidiness. `affects`
# omitted means one message and `reason` omitted means the verb is available;
# serialising either as an explicit null would say something the contract does
# not - and the picker reads presence, not value.
class ResolveChoice(BaseModel):
    choice: str
    label: str
    eligible: bool
    reason: str | None = None
    # Omitted when the verb touches exactly one message, and omission MEANS
    # one rather than unknown.
    affects: ResolveChoiceAffects | None = None


class ResolveCandidate(BaseModel):
    id: int
    company_name: str | None = None
    title: str | None = None
    applied_at: datetime.datetime | None = None


class ResolveMessage(BaseModel):
    id: int
    subject: str | None = None
    from_email: str | None = None
    sent_at: datetime.datetime | None = None
    classified_as: str | None = None
    extracted_company: str | None = None
    extracted_title: str | None = None


class ResolveItem(BaseModel):
    id: str
    kind: str
    rank: int
    rank_reason: str
    message: ResolveMessage
    candidates: list[ResolveCandidate]
    choices: list[ResolveChoice]


class ResolveQueue(BaseModel):
    items: list[ResolveItem]
    total: int
    # How many sit at each rank, so a page can say "40 need you, 3,623 do
    # not" rather than implying the first fifty are all there is.
    by_rank: dict[str, int]


class ResolveResult(BaseModel):
    ok: bool
    choice: str
    application_id: int | None = None


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


def choices_for_message(
    owner_id: int, message_id: int, company: str | None, thread: str | None
) -> list[dict[str, Any]]:
    """The verbs available on one message, decided HERE rather than by the
    caller.

    Shared with the candidate picker, which is the surface a person actually
    makes this decision on. A modal that builds the verb list itself has to
    decide eligibility client-side, and eligibility decided client-side is
    exactly what a server-declared contract exists to prevent - the first time
    a verb becomes conditional, one of the two lists is wrong and nothing says
    which.
    """
    key = mail_match.norm_company(company)
    has_candidate = False
    if key:
        rows = db.query(
            "SELECT company_name FROM applications WHERE user_id = %s AND dismissed_at IS NULL",
            (owner_id,),
        )
        has_candidate = any(mail_match.norm_company(r["company_name"]) == key for r in rows)
    if has_candidate:
        assign = _choice(
            ASSIGN,
            "Belongs to an application",
            messages=_thread_size(owner_id, message_id, thread),
        )
    else:
        assign = _choice(
            ASSIGN,
            "Belongs to an application",
            eligible=False,
            reason="no application at this company yet",
        )
    return [
        assign,
        _choice(NOT_AN_APPLICATION, "Belongs to no application"),
        _choice(NOT_JOB_RELATED, "Not job mail"),
    ]


# What a row is worth deciding, highest first. Ordering rather than scoring,
# and every step of it derived rather than weighted:
#
#   3  resolving it would MOVE a live application's stage
#   2  it can be attached to something, but attaching changes no stage
#   1  no application exists at that company yet, so only a refusal is available
#
# The top rank is the whole point. A rejection landing on an application still
# showing "applied" changes what the board says; an acknowledgement landing on
# the same application changes nothing, because stage is derived from the
# strongest event and an acknowledgement is never the strongest. Sorting by
# recency alone put those two side by side and let 2,884 year-old rows bury
# the forty that arrived this month.
#
# NOTHING IS HIDDEN. Every row is still returned and `total` still counts them
# all, because nothing in this population is unresolvable - a person can refuse
# any of it, so "low priority" is the honest claim and "cannot be settled" is
# not. That distinction belongs to action items, 157 of which have no event
# that can ever close them, and those get absence rather than a low rank when
# they become rows.
_RANK_MOVES_STAGE = 3
_RANK_ATTACHABLE = 2
_RANK_REFUSAL_ONLY = 1

_RANK_REASONS = {
    _RANK_MOVES_STAGE: "answering this moves an application",
    _RANK_ATTACHABLE: "can be attached, but the stage would not move",
    _RANK_REFUSAL_ONLY: "no application at this company yet",
}


def _rank(
    kind: str, candidates: list[dict[str, Any]], events: dict[int, list[dict[str, Any]]]
) -> int:
    if not candidates:
        return _RANK_REFUSAL_ONLY
    for app in candidates:
        own = events.get(app["id"], [])
        before = mail_pipeline.stage_for(own)
        if before in mail_pipeline.TERMINAL:
            continue
        # Would attaching this message move the stage? Asked of the same
        # function the board reads, over the application's real events, rather
        # than a second table saying which kinds count - so it cannot disagree
        # with what the board will show once the person answers.
        # `id` matters: stage_for breaks ties among terminal events by taking
        # the newest, so a hypothetical event has to look newer than the real
        # ones or a rejection already present would win over the one being
        # considered and the answer would be "changes nothing" for the exact
        # case that changes the most.
        newest = max((e.get("id") or 0) for e in own) if own else 0
        after = mail_pipeline.stage_for([*own, {"kind": kind, "sent_at": None, "id": newest + 1}])
        if after != before:
            return _RANK_MOVES_STAGE
    return _RANK_ATTACHABLE


def _queue_for(owner_id: int, limit: int, offset: int) -> dict[str, Any]:
    rows = db.query(
        _QUEUE_SQL,
        {
            "user": owner_id,
            "kinds": list(_AWAITING_KINDS),
            "refused": mail_match.NOT_AN_APPLICATION,
        },
    )
    if not rows:
        return {"items": [], "total": 0, "by_rank": {}}

    apps = db.query(
        "SELECT id, company_name, title, applied_at FROM applications "
        "WHERE user_id = %s AND dismissed_at IS NULL",
        (owner_id,),
    )
    events = mail_pipeline.events_by_application(owner_id)
    items = []
    for row in rows:
        key = mail_match.norm_company(row["company"])
        candidates = [a for a in apps if key and mail_match.norm_company(a["company_name"]) == key]
        rank = _rank(row["kind"], candidates, events)
        items.append(
            {
                "id": f"message:{row['id']}",
                "kind": "unmatched_message",
                "rank": rank,
                # Why it sits where it does, so the ordering is answerable
                # rather than something the page has to take on trust.
                "rank_reason": _RANK_REASONS[rank],
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
                    owner_id, row["id"], row["company"], row["provider_thread_id"]
                ),
            }
        )
    # RANKED BEFORE PAGED. Sorting inside a page would reorder fifty rows and
    # call it a ranking of three and a half thousand - the page would look
    # sensible and the ordering would be a lie.
    items.sort(key=lambda i: i["message"]["sent_at"] or _EPOCH, reverse=True)
    items.sort(key=lambda i: i["rank"], reverse=True)
    by_rank = Counter(i["rank"] for i in items)
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        # What is below the fold, so a page can say "40 need you, 3,623 do not"
        # rather than implying the first fifty are all there is.
        "by_rank": {_RANK_REASONS[k]: v for k, v in sorted(by_rank.items(), reverse=True)},
    }


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


@router.get("/user/resolve/queue", response_model=ResolveQueue, response_model_exclude_none=True)
def resolve_queue(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
):
    """Everything of the user's own awaiting a decision."""
    return _queue_for(user.id, limit, offset)


@router.post(
    "/user/resolve/{item_id}", response_model=ResolveResult, response_model_exclude_none=True
)
def resolve_item(item_id: str, body: ResolveRequest, user: AuthedUser = Depends(require_user)):
    return _resolve(item_id, body, owner_id=user.id, actor_user_id=user.id)


@router.get("/admin/resolve/queue", response_model=ResolveQueue, response_model_exclude_none=True)
def admin_resolve_queue(
    user_id: int = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_admin),
):
    """The same queue over another user's mail. Owner is a parameter; the
    caller's identity decides only whether they may ask."""
    return _queue_for(user_id, limit, offset)


@router.post(
    "/admin/resolve/{item_id}", response_model=ResolveResult, response_model_exclude_none=True
)
def admin_resolve_item(
    item_id: str,
    body: ResolveRequest,
    user_id: int = Query(...),
    user: AuthedUser = Depends(require_admin),
):
    return _resolve(item_id, body, owner_id=user_id, actor_user_id=user.id)
