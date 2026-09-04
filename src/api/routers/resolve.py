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

FOUR KINDS, ONE QUEUE. Unmatched mail was the only kind for as long as it was
the only one anybody could answer. The other three were already produced and
already unanswered: 4,674 attachments the matcher made with nobody ever asked
whether they were right, 1,159 status proposals on offer against 0 answers,
and 525 open action items. They were reachable through four more endpoint
families, which is the same shape this module was written to collapse, so they
belong here rather than beside it.
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
CONFIRM_MATCH = "confirm_match"
REJECT_MATCH = "reject_match"
ACCEPT_STATUS = "accept_status"
DECLINE_STATUS = "decline_status"
MARK_DONE = "mark_done"

# Where a verb's target comes from: the name of the field, ON THIS RESPONSE,
# holding the options. Not a global constant - the two surfaces that offer
# these verbs hold their applications under different keys, so each declares
# its own and a client reads `payload[choice.target_source]` without knowing
# which surface it is on.
CANDIDATES = "candidates"
PICKER_APPLICATIONS = "applications"

UNMATCHED_MESSAGE = "unmatched_message"
UNCONFIRMED_MATCH = "unconfirmed_match"
STATUS_PROPOSAL = "status_proposal"
ACTION_ITEM = "action_item"

# Every kind the queue can hold, in the order a caller sees them declared.
# Exposed as a filter value rather than as knowledge the client has to carry,
# so a fifth kind is a server change alone.
ITEM_KINDS = (UNMATCHED_MESSAGE, UNCONFIRMED_MATCH, STATUS_PROPOSAL, ACTION_ITEM)


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
    # Whether pressing this verb can be POSTed straight away or has to collect
    # a target first. Omitted means it takes no target.
    #
    # Without it a client has to know that `assign_application` is the verb
    # with an argument, which was the last piece of this vocabulary it was
    # still required to hardcode - and the point of declaring the verbs is that
    # a new decision type needs no client change. A verb that takes an argument
    # and cannot say so makes every client wrong the first time there are two
    # of them.
    needs_target: bool | None = None
    # WHERE the options come from, named rather than assumed. Every target
    # comes from the row's own `candidates` today; a verb that picked from
    # somewhere else would otherwise be a second silent assumption stacked on
    # the first.
    target_source: str | None = None


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


class ResolveApplication(BaseModel):
    """The application a row is about, with the stage the board would show.

    `stage` is `mail_pipeline.stage_for` over that application's real events,
    not a second reading of them, so the queue and the board cannot disagree
    about what an application is doing while asking about it.
    """

    id: int
    company_name: str | None = None
    title: str | None = None
    stage: str | None = None
    on_board: bool = False


class ResolveImplication(BaseModel):
    """What answering would change beyond the row itself.

    Present only where there is something to say. A control whose effect
    reaches past the row states that before the click rather than reporting it
    afterwards, and `board_updated` is the honest half of that: an application
    with no board row has a status to propose and nothing to move.
    """

    board_status: str
    from_status: str | None = None
    board_updated: bool
    reason: str | None = None


class ResolveMatch(BaseModel):
    id: int
    method: str
    confidence: str | None = None
    rationale: str | None = None
    created_at: datetime.datetime | None = None


class ResolveAction(BaseModel):
    id: int
    kind: str
    due_at: datetime.datetime | None = None
    # What could ever close this without a person. Empty means nothing can,
    # which is why the item is here rather than waiting on the next email.
    settles_on: list[str]


class ResolveItem(BaseModel):
    id: str
    kind: str
    rank: int
    rank_reason: str
    choices: list[ResolveChoice]
    message: ResolveMessage | None = None
    candidates: list[ResolveCandidate] | None = None
    application: ResolveApplication | None = None
    implies: ResolveImplication | None = None
    match: ResolveMatch | None = None
    action: ResolveAction | None = None


class ResolveQueue(BaseModel):
    items: list[ResolveItem]
    total: int
    # How many sit at each rank, so a page can say "40 need you, 3,623 do
    # not" rather than implying the first fifty are all there is.
    by_rank: dict[str, int]
    # The same honesty per kind, which is what makes the one queue readable as
    # the four questions it merges rather than as an undifferentiated pile.
    by_kind: dict[str, int]


class ResolveResult(BaseModel):
    ok: bool
    choice: str
    application_id: int | None = None
    # What the answer actually touched. Omitted where the verb touches nothing
    # beyond the row, present and false where it was meant to and could not.
    board_updated: bool | None = None
    board_status: str | None = None
    reason: str | None = None


class ResolveRequest(BaseModel):
    choice: Literal[
        "assign_application",
        "not_an_application",
        "not_job_related",
        "confirm_match",
        "reject_match",
        "accept_status",
        "decline_status",
        "mark_done",
    ]
    target: int | None = None
    note: str | None = None


class DecisionRow(BaseModel):
    id: str
    at: datetime.datetime
    kind: str
    decision: str
    by: str
    summary: str
    application_id: int | None = None
    # The decision this one replaced, and whether something later replaced
    # THIS one. An overturned answer that vanishes takes the evidence that the
    # rule was wrong with it.
    superseded_by: str | None = None
    supersedes: str | None = None


class DecisionHistory(BaseModel):
    decisions: list[DecisionRow]
    total: int


def _choice(
    choice: str,
    label: str,
    *,
    eligible: bool = True,
    reason: str | None = None,
    messages: int = 1,
    target_source: str | None = None,
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
    if target_source:
        # The two travel together deliberately. A verb that says it needs a
        # target without saying where the options come from has moved the
        # guess rather than removed it.
        out["needs_target"] = True
        out["target_source"] = target_source
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
       a.company_name, a.title, uj.user_id IS NOT NULL AS on_board
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
       a.company_name, a.title, uj.user_id IS NOT NULL AS on_board,
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


def _thread_sizes(user_id: int) -> dict[str, int]:
    """How many messages each conversation holds, in one query.

    Asked per message it was one round trip per row, and the queue ranks
    before it pages - so a fifty-row page cost a query for every one of the
    3,251 rows behind it. The count is a property of the thread, not of the
    message, so it is one GROUP BY.
    """
    return {
        row["provider_thread_id"]: int(row["c"])
        for row in db.query(
            "SELECT provider_thread_id, count(*) AS c FROM email_messages "
            "WHERE user_id = %s AND provider_thread_id IS NOT NULL "
            "GROUP BY provider_thread_id",
            (user_id,),
        )
    }


def thread_size(user_id: int, thread: str | None) -> int:
    """How many messages one assign would actually move.

    Assign carries the whole conversation by default, so the count belongs in
    the button rather than in the response afterwards - a person deciding one
    message should know before clicking that it moves fourteen. For a whole
    queue page use `_thread_sizes`, which asks once instead of once per row.
    """
    if not thread:
        return 1
    row = db.query_one(
        "SELECT count(*) AS c FROM email_messages WHERE user_id = %s AND provider_thread_id = %s",
        (user_id, thread),
    )
    return max(1, int((row or {}).get("c", 1)))


def by_company(apps: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Applications indexed by normalised company, built once per request.

    The queue ranks before it pages, so every helper below ran over the whole
    application list for every one of 3,251 rows - 8.2 million `norm_company`
    calls, each two regex substitutions, to answer a question that has 2,543
    distinct answers. Normalising each side once is the same predicate, and it
    is the ONLY place the two sides may be compared: `norm_company` is what
    makes "Stripe" and "Stripe, Inc." one employer, and an index keyed on raw
    text would silently be a stricter matcher than the one it stands in for.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for app in apps:
        key = mail_match.norm_company(app["company_name"])
        if key:
            index.setdefault(key, []).append(app)
    return index


def choices_for_message(
    apps_by_company: dict[str, list[dict[str, Any]]],
    company: str | None,
    thread_size: int,
    target_source: str,
) -> list[dict[str, Any]]:
    """The verbs available on one message, decided HERE rather than by the
    caller.

    Shared with the candidate picker, which is the surface a person actually
    makes this decision on. A modal that builds the verb list itself has to
    decide eligibility client-side, and eligibility decided client-side is
    exactly what a server-declared contract exists to prevent - the first time
    a verb becomes conditional, one of the two lists is wrong and nothing says
    which.

    Takes the index, the thread size and the name of its own target list rather
    than fetching or assuming them. Required, not defaulted: a default is how
    "I did not have this" hides inside shared code as if it were "there is
    nothing", and all three are already in hand at every call site.

    `target_source` IS A PER-SURFACE FACT and that is why the caller states it.
    Two surfaces share these verbs and they hold their applications under
    different keys - the queue row calls the list `candidates`, the picker
    calls it `applications`. A constant here would name whichever one was
    written first and be wrong on the other, which is the same hardcoded fact
    the field exists to remove, moved one module along.

    ELIGIBILITY IS "AN APPLICATION EXISTS AT THIS COMPANY", not "the list in
    front of you is non-empty". On the queue those coincide, because the row's
    candidates and this index are the same set read under the same key. On the
    picker they do not: its list is search-filtered, and typing a query that
    matches nothing does not stop the application existing. So the caller that
    wants the stronger claim - eligible exactly when its own list is non-empty
    - is the queue, and it holds there by construction rather than by promise.
    """
    key = mail_match.norm_company(company)
    if key and apps_by_company.get(key):
        assign = _choice(
            ASSIGN,
            "Belongs to an application",
            messages=thread_size,
            target_source=target_source,
        )
    else:
        assign = _choice(
            ASSIGN,
            "Belongs to an application",
            eligible=False,
            reason="no application at this company yet",
            target_source=target_source,
        )
    return [
        assign,
        _choice(NOT_AN_APPLICATION, "Belongs to no application"),
        _choice(NOT_JOB_RELATED, "Not job mail"),
    ]


# What a row is worth deciding, highest first. Ordering rather than scoring,
# and every step of it derived rather than weighted:
#
#   3  answering it changes what the product says
#   2  it can be answered, but nothing changes on its own
#   1  only a refusal is available
#
# The top rank is the whole point. A rejection landing on an application still
# showing "applied" changes what the board says; an acknowledgement landing on
# the same application changes nothing, because stage is derived from the
# strongest event and an acknowledgement is never the strongest. Sorting by
# recency alone put those two side by side and let 2,884 year-old rows bury
# the forty that arrived this month.
#
# The same question is asked of every kind, which is what lets one number order
# four of them: would answering this change something a person would see.
#
# NOTHING IS HIDDEN. Every row is still returned and `total` still counts them
# all, because nothing in this population is unresolvable - a person can refuse
# any of it, so "low priority" is the honest claim and "cannot be settled" is
# not. That distinction belongs to action items, and they carry it as
# `settles_on` rather than as a rank, because measured over the corpus a rank
# could not carry it: the only kind with an empty settling set is
# `reply_to_recruiter`, and all 73 of those are already closed. Ranking on it
# would have looked principled and sorted nothing.
_RANK_MOVES_STAGE = 3
_RANK_ATTACHABLE = 2
_RANK_REFUSAL_ONLY = 1

# The bucket labels `by_rank` is keyed by. Kind-neutral, because the queue holds
# four kinds and three of them are not about attaching anything - a bucket
# labelled "can be attached" would misdescribe every action item in it. The
# specific sentence lives on the row, in `rank_reason`.
_RANK_LABELS = {
    _RANK_MOVES_STAGE: "answering this changes what the product says",
    _RANK_ATTACHABLE: "answerable, but nothing changes on its own",
    _RANK_REFUSAL_ONLY: "only a refusal is available",
}

# Why one unmatched message sits where it does, which is a narrower claim than
# the bucket it lands in.
_MESSAGE_RANK_REASONS = {
    _RANK_MOVES_STAGE: "answering this moves an application",
    _RANK_ATTACHABLE: "can be attached, but the stage would not move",
    _RANK_REFUSAL_ONLY: "no application at this company yet",
}


def _stage_would_move(kind: str | None, own: list[dict[str, Any]]) -> bool:
    """Would adding an event of this kind change this application's stage?

    Asked of the same function the board reads, over the application's real
    events, rather than a second table saying which kinds count - so it cannot
    disagree with what the board will show once the person answers.

    `id` matters: stage_for breaks ties among terminal events by taking the
    newest, so a hypothetical event has to look newer than the real ones or a
    rejection already present would win over the one being considered and the
    answer would be "changes nothing" for the exact case that changes the most.
    """
    if not kind:
        return False
    before = mail_pipeline.stage_for(own)
    if before in mail_pipeline.TERMINAL:
        return False
    newest = max((e.get("id") or 0) for e in own) if own else 0
    after = mail_pipeline.stage_for([*own, {"kind": kind, "sent_at": None, "id": newest + 1}])
    return after != before


def _rank(
    kind: str, candidates: list[dict[str, Any]], events: dict[int, list[dict[str, Any]]]
) -> int:
    if not candidates:
        return _RANK_REFUSAL_ONLY
    for app in candidates:
        if _stage_would_move(kind, events.get(app["id"], [])):
            return _RANK_MOVES_STAGE
    return _RANK_ATTACHABLE


def _message_items(
    owner_id: int,
    apps_by_company: dict[str, list[dict[str, Any]]],
    events: dict[int, list[dict[str, Any]]],
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
        rank = _rank(row["kind"], candidates, events)
        items.append(
            {
                "id": f"message:{row['id']}",
                "kind": UNMATCHED_MESSAGE,
                "rank": rank,
                # Why it sits where it does, so the ordering is answerable
                # rather than something the page has to take on trust.
                "rank_reason": _MESSAGE_RANK_REASONS[rank],
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


def _match_items(owner_id: int, events: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Attachments the matcher made that nobody has been asked about.

    The stage a rejection would remove is the same question `_rank` asks of an
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
        without = [e for e in own if e.get("message_id") != row["message_id"]]
        moves = mail_pipeline.stage_for(without) != mail_pipeline.stage_for(own)
        rank = _RANK_MOVES_STAGE if moves else _RANK_ATTACHABLE
        implies = None
        if row["kind"] in mail_pipeline.STATUS_FROM_EVENT:
            implies = {
                "board_status": mail_pipeline.STATUS_FROM_EVENT[row["kind"]],
                "board_updated": bool(row["on_board"]),
                "reason": None
                if row["on_board"]
                else "this application is not on your board, so no status would move",
            }
        items.append(
            {
                "id": f"match:{row['match_id']}",
                "kind": UNCONFIRMED_MATCH,
                "rank": rank,
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


def _proposal_items(owner_id: int) -> list[dict[str, Any]]:
    """Where the mail and the board disagree.

    Every one of these moves what the product says, by construction - a
    proposal is only made where the board still says the application is live
    and the mail says it is not. So they all rank at the top, and the reason
    says which way.
    """
    items = []
    for row in mail_pipeline.proposals_for(owner_id):
        items.append(
            {
                "id": f"proposal:{row['application_id']}:{row['event_id']}",
                "kind": STATUS_PROPOSAL,
                "rank": _RANK_MOVES_STAGE,
                "rank_reason": "the mail and your board disagree about this application",
                "message": {"id": row["message_id"], "sent_at": row["sent_at"]},
                "application": {
                    "id": row["application_id"],
                    "company_name": row["company_name"],
                    "title": row["title"],
                    "stage": None,
                    "on_board": bool(row["board_updatable"]),
                },
                "implies": {
                    "board_status": row["suggested_status"],
                    "from_status": row["board_status"],
                    "board_updated": bool(row["board_updatable"]),
                    "reason": row["board_reason"],
                },
                "choices": [
                    _choice(ACCEPT_STATUS, f"Move it to {row['suggested_status']}"),
                    _choice(DECLINE_STATUS, "Leave it where it is"),
                ],
            }
        )
    return items


def _action_items(owner_id: int) -> list[dict[str, Any]]:
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
                "rank": _RANK_ATTACHABLE,
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
                    "stage": None,
                    "on_board": bool(row["on_board"]),
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


def _queue_for(
    owner_id: int, limit: int, offset: int, kinds: list[str] | None = None
) -> dict[str, Any]:
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
        items += _proposal_items(owner_id)
    if ACTION_ITEM in wanted:
        items += _action_items(owner_id)

    # RANKED BEFORE PAGED. Sorting inside a page would reorder fifty rows and
    # call it a ranking of three and a half thousand - the page would look
    # sensible and the ordering would be a lie.
    items.sort(key=lambda i: (i.get("message") or {}).get("sent_at") or _EPOCH, reverse=True)
    items.sort(key=lambda i: i["rank"], reverse=True)
    by_rank = Counter(i["rank"] for i in items)
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        # What is below the fold, so a page can say "40 need you, 3,623 do not"
        # rather than implying the first fifty are all there is.
        "by_rank": {_RANK_LABELS[k]: v for k, v in sorted(by_rank.items(), reverse=True)},
        # Counted over everything asked for, never over the page.
        "by_kind": dict(Counter(i["kind"] for i in items)),
    }


def _owned_message(message_id: int, owner_id: int) -> None:
    row = db.query_one(
        "SELECT id FROM email_messages WHERE id = %s AND user_id = %s", (message_id, owner_id)
    )
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})


def _resolve_message(
    message_id: int, body: ResolveRequest, owner_id: int, actor_user_id: int
) -> dict:
    _owned_message(message_id, owner_id)

    if body.choice == ASSIGN:
        if body.target is None:
            raise HTTPException(
                400, detail={"code": "TARGET_REQUIRED", "message": "assign needs an application"}
            )
        # `dismissed_at IS NULL` is the same predicate the verb's eligibility is
        # declared from, ENFORCED HERE rather than only announced. Without it
        # the server said "no application at this company yet" for a dismissed
        # one and then accepted it as a target anyway, so the declaration was
        # decoration: a client that ignored `eligible` got its way, and mail
        # landed on an application whose whole meaning is that it should never
        # have existed.
        owner = db.query_one(
            "SELECT id, dismissed_at FROM applications WHERE id = %s AND user_id = %s",
            (body.target, owner_id),
        )
        if owner is None:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown application"})
        if owner["dismissed_at"] is not None:
            # 409 rather than 404: it exists and the caller may see it, but the
            # state it is in refuses the verb, and saying so is what lets them
            # restore it instead of guessing at a missing row.
            raise HTTPException(
                409,
                detail={
                    "code": "DISMISSED",
                    "message": "that application is dismissed; restore it before assigning to it",
                },
            )
        mail_match.record(
            message_id,
            mail_match.Match(
                body.target, mail_match.MANUAL, "high", body.note or "resolved by hand"
            ),
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


def _resolve_match(match_id: int, body: ResolveRequest, owner_id: int, actor_user_id: int) -> dict:
    """Confirm or reject one attachment.

    Bound to the OWNER through the application, not just to the match id.
    Owning a parent says nothing about owning a child, and a match id taken
    from the request and trusted would let anyone's attachment be answered.

    Answering the row that is no longer current is refused rather than
    silently applied. A queue page can be minutes old, and confirming an
    attachment that has since been replaced would write back a decision about
    a world that is gone.
    """
    row = db.query_one(
        """
        SELECT am.id, am.message_id, am.application_id, am.method, am.confidence,
               (am.id = (SELECT max(id) FROM application_matches
                         WHERE message_id = am.message_id)) AS is_current
        FROM application_matches am
        JOIN applications a ON a.id = am.application_id
        WHERE am.id = %s AND a.user_id = %s
        """,
        (match_id, owner_id),
    )
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
    if not row["is_current"]:
        raise HTTPException(
            409,
            detail={
                "code": "STALE",
                "message": "this attachment has already been superseded; reload the queue",
            },
        )

    if body.choice == CONFIRM_MATCH:
        mail_match.confirm(row["message_id"], row, actor_user_id=actor_user_id, note=body.note)
        return {"ok": True, "choice": body.choice, "application_id": row["application_id"]}

    mail_match.reject(row["message_id"], actor_user_id=actor_user_id, note=body.note)
    # The events this message carried stop reaching the application, so
    # anything they opened has to follow rather than sit there asking about an
    # application it is no longer part of.
    mail_pipeline.sync_action_items(row["application_id"])
    return {"ok": True, "choice": body.choice}


def _resolve_proposal(
    application_id: int, event_id: int, body: ResolveRequest, owner_id: int
) -> dict:
    answered = mail_pipeline.answer_proposal(
        owner_id,
        application_id,
        event_id,
        mail_pipeline.ACCEPTED if body.choice == ACCEPT_STATUS else mail_pipeline.DISMISSED,
    )
    if answered is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
    return {
        "ok": True,
        "choice": body.choice,
        "application_id": application_id,
        "board_updated": answered["board_updated"],
        "board_status": answered["board_status"],
        "reason": answered["reason"],
    }


def _resolve_action(action_id: int, body: ResolveRequest, owner_id: int) -> dict:
    row = db.query_one(
        "SELECT id, resolved_at FROM action_items WHERE id = %s AND user_id = %s",
        (action_id, owner_id),
    )
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
    if row["resolved_at"] is None:
        db.execute(
            "UPDATE action_items SET resolved_at = now(), resolution = %s WHERE id = %s",
            (body.note or "marked done", action_id),
        )
    return {"ok": True, "choice": body.choice}


# Which verbs each item kind accepts. Declared once and checked here, so a verb
# offered on the wrong kind is a 400 rather than an operation that half runs.
_CHOICES_BY_KIND = {
    "message": {ASSIGN, NOT_AN_APPLICATION, NOT_JOB_RELATED},
    "match": {CONFIRM_MATCH, REJECT_MATCH},
    "proposal": {ACCEPT_STATUS, DECLINE_STATUS},
    "action": {MARK_DONE},
}


def _resolve(item_id: str, body: ResolveRequest, owner_id: int, actor_user_id: int) -> dict:
    kind, _, raw = item_id.partition(":")
    parts = raw.split(":")
    if kind not in _CHOICES_BY_KIND or not all(p.isdigit() for p in parts):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})
    if body.choice not in _CHOICES_BY_KIND[kind]:
        raise HTTPException(
            400,
            detail={
                "code": "WRONG_CHOICE",
                "message": f"{body.choice} is not a verb on a {kind} item",
            },
        )

    if kind == "message" and len(parts) == 1:
        return _resolve_message(int(parts[0]), body, owner_id, actor_user_id)
    if kind == "match" and len(parts) == 1:
        return _resolve_match(int(parts[0]), body, owner_id, actor_user_id)
    if kind == "proposal" and len(parts) == 2:
        return _resolve_proposal(int(parts[0]), int(parts[1]), body, owner_id)
    if kind == "action" and len(parts) == 1:
        return _resolve_action(int(parts[0]), body, owner_id)
    raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown queue item"})


# Everything a person has decided, from the four logs that record it, newest
# first.
#
# Read from the logs themselves rather than from a decisions table, because
# every one of these is already append-only and a fifth copy would be the one
# that drifts. A decision that vanishes when it is reversed takes the evidence
# that the rule was wrong with it, so both survive and each says which it
# replaced.
#
# The neighbours are computed over HUMAN rows only, which is why the filter
# sits inside each branch rather than outside the window. Windowing over every
# row would point `supersedes` at the matcher's own attachment - a real row,
# but not a decision, and not in this list. Every id either side of a decision
# here resolves to another row in the same response.
#
# That the matcher can never appear as the newer row is not an accident of the
# data: `mail_match.record` refuses to overwrite a human verdict, so a human
# row's successor is always another human row.
_HISTORY_SQL = """
WITH match_decisions AS (
    SELECT am.id, am.created_at, am.actor_user_id, am.message_id, am.application_id,
           am.rationale
    FROM application_matches am
    JOIN email_messages m ON m.id = am.message_id
    WHERE m.user_id = %(user)s AND am.actor_user_id IS NOT NULL
)
SELECT 'match' AS log, d.id, d.created_at AS at, d.actor_user_id, d.application_id,
       lead(d.id) OVER (PARTITION BY d.message_id ORDER BY d.id) AS newer,
       lag(d.id) OVER (PARTITION BY d.message_id ORDER BY d.id) AS older,
       CASE WHEN d.application_id IS NOT NULL THEN 'attached' ELSE 'rejected' END AS decision,
       coalesce(a.company_name, m.subject, 'a message') AS subject
FROM match_decisions d
JOIN email_messages m ON m.id = d.message_id
LEFT JOIN applications a ON a.id = d.application_id

UNION ALL

SELECT 'proposal', sr.id, sr.created_at, sr.user_id, sr.application_id,
       lead(sr.id) OVER (PARTITION BY sr.application_id, sr.event_id ORDER BY sr.id),
       lag(sr.id) OVER (PARTITION BY sr.application_id, sr.event_id ORDER BY sr.id),
       sr.response,
       coalesce(a.company_name, 'an application')
FROM suggestion_responses sr
LEFT JOIN applications a ON a.id = sr.application_id
WHERE sr.user_id = %(user)s

UNION ALL

-- Closed by hand, not by a later email. `resolved_by_event_id` set means the
-- system settled it, which is it working rather than a decision anyone made.
--
-- No neighbours, because `action_items` is updated in place rather than
-- appended to: reopening clears `resolved_at` and the closure that preceded it
-- is gone from the row. So this log can show that an item was closed and
-- cannot show that it was closed twice. Stated rather than papered over - the
-- fix is a second table and it is not worth one for the 0 items a person has
-- ever closed.
SELECT 'action', ai.id, ai.resolved_at, ai.user_id, ai.application_id, NULL, NULL,
       'closed', coalesce(a.company_name, ai.kind)
FROM action_items ai
LEFT JOIN applications a ON a.id = ai.application_id
WHERE ai.user_id = %(user)s
  AND ai.resolved_at IS NOT NULL
  AND ai.resolved_by_event_id IS NULL

UNION ALL

SELECT 'classification', ev.id, ev.created_at, ev.actor_user_id, NULL,
       lead(ev.id) OVER (PARTITION BY ev.message_id ORDER BY ev.id),
       lag(ev.id) OVER (PARTITION BY ev.message_id ORDER BY ev.id),
       ev.kind, coalesce(m2.subject, 'a message')
FROM email_events ev
JOIN email_messages m2 ON m2.id = ev.message_id
WHERE m2.user_id = %(user)s AND ev.actor_user_id IS NOT NULL

ORDER BY at DESC, id DESC
"""


def _history_for(owner_id: int, viewer_id: int, limit: int, offset: int) -> dict[str, Any]:
    rows = db.query(_HISTORY_SQL, {"user": owner_id})
    decisions = [
        {
            "id": f"{row['log']}:{row['id']}",
            "at": row["at"],
            "kind": row["log"],
            "decision": row["decision"],
            # The same actor id reads as "you" to the owner and as an
            # administrator to anyone else, derived rather than stored twice.
            "by": "you" if row["actor_user_id"] == viewer_id else "administrator",
            "summary": row["subject"],
            "application_id": row["application_id"],
            "superseded_by": f"{row['log']}:{row['newer']}" if row["newer"] else None,
            "supersedes": f"{row['log']}:{row['older']}" if row["older"] else None,
        }
        for row in rows
    ]
    return {"decisions": decisions[offset : offset + limit], "total": len(decisions)}


@router.get("/user/resolve/queue", response_model=ResolveQueue, response_model_exclude_none=True)
def resolve_queue(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    kind: list[str] | None = Query(default=None),
    user: AuthedUser = Depends(require_user),
):
    """Everything of the user's own awaiting a decision, of every kind.

    `kind` narrows it, repeated for several. Unfiltered is the default because
    "what is waiting on me" is the question this answers, and four separate
    answers to it is the shape it replaced.
    """
    if kind and set(kind) - set(ITEM_KINDS):
        raise HTTPException(
            400,
            detail={
                "code": "UNKNOWN_KIND",
                "message": f"kind must be one of {', '.join(ITEM_KINDS)}",
            },
        )
    return _queue_for(user.id, limit, offset, kind)


@router.get(
    "/user/resolve/history", response_model=DecisionHistory, response_model_exclude_none=True
)
def resolve_history(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
):
    """What the user has decided, newest first, overturned answers included."""
    return _history_for(user.id, user.id, limit, offset)


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
    kind: list[str] | None = Query(default=None),
    user: AuthedUser = Depends(require_admin),
):
    """The same queue over another user's mail. Owner is a parameter; the
    caller's identity decides only whether they may ask."""
    return _queue_for(user_id, limit, offset, kind)


@router.get(
    "/admin/resolve/history", response_model=DecisionHistory, response_model_exclude_none=True
)
def admin_resolve_history(
    user_id: int = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_admin),
):
    """One user's decisions, as an administrator sees them.

    `by` is computed against the CALLER, so the administrator's own
    corrections read as "you" and the owner's read as somebody else - the
    opposite of what the owner sees for the same rows, and correct for both.
    """
    return _history_for(user_id, user.id, limit, offset)


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


class ReviewRate(BaseModel):
    method: str
    confidence: str | None = None
    attached: int
    reviewed: int
    confirmed: int
    rejected: int
    # NULL, not zero. A tier nobody has reviewed has no rate, and rendering
    # that as 0% says the tier is always wrong.
    confirm_rate: float | None = None
    note: str | None = None


class ReviewRates(BaseModel):
    by_method: list[ReviewRate]
    never_reviewed: int
    reviewed: int


# Confirm and reject rates per tier. The GROUPING IS THE POINT: "is the matcher
# right" is unanswerable, while "is ats_company at medium confidence right"
# decides whether that tier should keep writing unattended. It only works
# because confirming preserves the method the matcher wrote instead of
# restamping it `manual`.
#
# Rejections are counted against the tier that MADE the attachment, which is
# the row underneath the rejection rather than the rejection itself - a
# `detached` row names no tier, so counting by its own method would put every
# rejection in one bucket and no tier would ever look wrong.
_REVIEW_RATES_SQL = """
WITH ranked AS (
    SELECT am.id, am.message_id, am.application_id, am.method, am.confidence,
           am.actor_user_id,
           row_number() OVER (PARTITION BY am.message_id ORDER BY am.id DESC) AS rn,
           lag(am.method) OVER (PARTITION BY am.message_id ORDER BY am.id) AS prev_method,
           lag(am.confidence) OVER (PARTITION BY am.message_id ORDER BY am.id) AS prev_confidence,
           lag(am.application_id) OVER (PARTITION BY am.message_id ORDER BY am.id) AS prev_app,
           lag(am.actor_user_id) OVER (PARTITION BY am.message_id ORDER BY am.id) AS prev_actor
    FROM application_matches am
    JOIN email_messages m ON m.id = am.message_id
    -- NULL means every user, which is what /job-scripts is: the view across
    -- the fleet, not one person's data behind a permission level.
    WHERE (%(user)s::bigint IS NULL OR m.user_id = %(user)s)
),
attached AS (
    SELECT method, confidence,
           count(*) AS attached,
           count(*) FILTER (WHERE actor_user_id IS NOT NULL) AS reviewed
    FROM ranked WHERE rn = 1 AND application_id IS NOT NULL
    GROUP BY 1, 2
),
answered AS (
    -- A human row whose predecessor was the matcher's. Same application means
    -- they agreed; a NULL application means they threw it out.
    SELECT prev_method AS method, prev_confidence AS confidence,
           count(*) FILTER (WHERE application_id IS NOT DISTINCT FROM prev_app) AS confirmed,
           count(*) FILTER (WHERE application_id IS NULL AND prev_app IS NOT NULL) AS rejected
    FROM ranked
    WHERE actor_user_id IS NOT NULL AND prev_actor IS NULL AND prev_app IS NOT NULL
    GROUP BY 1, 2
)
SELECT coalesce(att.method, ans.method) AS method,
       coalesce(att.confidence, ans.confidence) AS confidence,
       coalesce(att.attached, 0) AS attached,
       coalesce(att.reviewed, 0) AS reviewed,
       coalesce(ans.confirmed, 0) AS confirmed,
       coalesce(ans.rejected, 0) AS rejected
FROM attached att
FULL OUTER JOIN answered ans
  ON ans.method = att.method AND ans.confidence IS NOT DISTINCT FROM att.confidence
ORDER BY 3 DESC, 1
"""


@router.get("/admin/resolve/rates", response_model=ReviewRates, response_model_exclude_none=True)
def admin_review_rates(
    user_id: int | None = Query(default=None), user: AuthedUser = Depends(require_admin)
):
    """How often a person agrees with each tier, and how much nobody has read.

    `user_id` is OPTIONAL and omitting it means the whole fleet. /job-scripts
    is the view across all users rather than one user's data with a permission
    level on it, so "is `ats_company` right" is the question it exists to ask -
    and a required owner made the fleet-wide form of it unaskable, which is the
    only form that decides whether a tier keeps writing unattended.

    Read-only, and deliberately without the verbs. An administrator answering
    somebody else's match is a different act from the owner answering it, and
    it already has a home on the resolve routes above where `actor_user_id`
    records which of the two happened. What this surface is for is deciding
    whether a tier should keep writing unattended, which is a question about
    the aggregate rather than about any row.
    """
    rows = db.query(_REVIEW_RATES_SQL, {"user": user_id})
    by_method = []
    for row in rows:
        answered = row["confirmed"] + row["rejected"]
        by_method.append(
            {
                **row,
                "confirm_rate": (row["confirmed"] / answered) if answered else None,
                "note": None if answered else "not measured: nobody has reviewed this tier yet",
            }
        )
    return {
        "by_method": by_method,
        "never_reviewed": sum(r["attached"] - r["reviewed"] for r in rows),
        "reviewed": sum(r["reviewed"] for r in rows),
    }
