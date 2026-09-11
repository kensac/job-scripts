"""A person's own mail: what arrived, what it meant, where it went.

Not the debug view. An admin asks which messages the pipeline handled badly; a
person asks what arrived and where it went, and reading your own inbox should
not require the permission to read everyone's.

A conversation is the unit a person thinks in, so the thread reader and the
thread list are here rather than beside the messages they group.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.routers.mail.shared import (
    Candidates,
    Reclassification,
    Reclassified,
    Reverted,
    _apply_classification,
    _apply_revert,
    _candidates_payload,
)
from core.answers import EVENT_KINDS
from core.mail.html import sanitise

router = APIRouter()


# How many messages one assignment may carry. A conversation is a handful of
# messages; a number far above that means the grouping is wrong rather than the
# thread being long, and silently reassigning hundreds of messages on one click
# is the failure worth refusing.
MAX_THREAD_FANOUT = 40

# A conversation's key, derived rather than stored.
#
# `provider_thread_id` is the FIRST References entry, which is the Message-ID
# of the message that started the thread. So replies carry it and the ROOT does
# not - a first message has no References, by definition. That left 1,113
# messages as the origin of a thread that exists in the database without being
# part of it: assigning a reply moved its siblings and silently left the
# original behind.
#
# Coalescing to the message's own id closes it, because a root's Message-ID IS
# the key its replies carry. A message with no thread and no replies coalesces
# to its own id and matches nothing else, so this cannot group unrelated mail -
# which is the failure that made subject-based grouping unsafe.
_THREAD_KEY = "COALESCE(m.provider_thread_id, m.provider_message_id)"


class Assignment(BaseModel):
    """One of three targets. An application to attach to, a board job to
    create an application from, or a company and title when neither exists -
    which is the common case for mail predating the catalog."""

    application_id: int | None = None
    job_id: int | None = None
    company_name: str | None = None
    title: str | None = None
    note: str | None = None
    # Whether to carry the whole conversation. Default on, because a person
    # correcting one message of a thread means the thread, and making them do
    # it five times is the kind of chore this system exists to remove.
    whole_thread: bool = True


def _owned_message(message_id: int, user_id: int) -> dict[str, Any]:
    row = db.query_one(
        "SELECT id, subject, from_email, sent_at, body_text, provider_thread_id "
        "FROM email_messages WHERE id = %s AND user_id = %s",
        (message_id, user_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="message not found")
    return row


_USER_MAIL_SORTS = {
    "sent_at": "m.sent_at",
    "from_email": "lower(m.from_email)",
    "subject": "lower(m.subject)",
    "kind": "ce.kind",
    "company": "lower(coalesce(a.company_name, ce.detail->>'company'))",
}


class MailMessage(BaseModel):
    """One of a person's own messages: what arrived, what it was read as, and
    where it went.

    Not the debug row. An admin asks which messages the pipeline handled
    badly and needs the prefilter and the model; a person asks what arrived
    and which application it reached."""

    id: int
    subject: str | None
    from_email: str | None
    sent_at: datetime.datetime | None
    source: str
    kind: str | None
    confidence: str | None
    extracted_company: str | None
    application_id: int | None
    method: str | None
    company_name: str | None
    title: str | None


class UserMail(BaseModel):
    """The page, and the per-kind counts over the SAME predicate - so a tab's
    number and its contents cannot disagree."""

    messages: list[MailMessage]
    total: int
    has_more: bool
    by_kind: dict[str, int]


@router.get("/user/mail")
def user_mail(
    kind: str | None = Query(default=None),
    matched: bool | None = Query(default=None),
    classified: bool | None = Query(default=None),
    application_id: int | None = Query(default=None),
    sort: str = Query(default="sent_at"),
    dir: str = Query(default="desc"),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
) -> UserMail:
    """The user's own mail, and what the pipeline did with each message.

    There was no way to see this without being an admin. `/admin/mail` is the
    debug view - it exists to answer "why did the classifier decide that", it
    spans every user, and it is gated behind infra-admin. Reading your own
    inbox should not require the permission to read everyone's.

    The two surfaces answer different questions and so are not the same query.
    An admin asks which messages the pipeline handled badly; a person asks what
    arrived and where it went. This returns the second: the current
    classification, whether it reached an application, and which one.
    """
    where = ["m.user_id = %(user)s"]
    params: dict[str, Any] = {"user": user.id, "limit": limit, "offset": offset}
    if kind is None and classified is None and application_id is None:
        # The default lens is job mail, not the mailbox. Nothing is filtered at
        # ingest, on purpose (core/mail_prefilter.py): a missed job email is
        # unrecoverable. That is a rule about what to STORE, and it was being
        # read as a rule about what to show - 58,201 of 67,226 messages
        # (86.6%, measured 2026-09-03) classify as not_job_related, so the
        # unfiltered default handed a person their whole mailbox.
        #
        # Nothing becomes unreachable: kind=not_job_related returns all 58,201
        # and classified=false returns the 158 nothing has looked at yet.
        # Naming any of those three narrowings replaces this lens rather than
        # stacking with it, which is why application_id is exempt - 638 matched
        # messages are classified not_job_related, and dropping them would make
        # "the messages behind this application" quietly untrue.
        where.append("ce.kind IS NOT NULL AND ce.kind <> 'not_job_related'")
    if kind:
        # Comma-separated for the same reason as stage: "replies" is rejection
        # AND offer AND interview_invite AND assessment_invite, and a lens the
        # client assembles from four requests is not the same set.
        kinds = [k.strip() for k in kind.split(",") if k.strip()]
        where.append("ce.kind = ANY(%(kinds)s)")
        params["kinds"] = kinds
    if q:
        where.append("(m.subject ILIKE %(q)s OR m.from_email ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if classified is not None:
        # The backlog, reachable by its own name. Excluded from the unmatched
        # queue because nothing has looked at it yet, which is a different
        # state from the matcher having failed - and a queue that mixes them
        # asks a person to fix something no decision has been made about.
        where.append("ce.kind IS NOT NULL" if classified else "ce.kind IS NULL")
    if application_id is not None:
        # The reverse trip. Without it an application can only link to a
        # company text search, which returns a DIFFERENT set and would quietly
        # lie about being "the messages behind this application".
        where.append("cm.application_id = %(app)s")
        params["app"] = application_id
    if matched is True:
        where.append("cm.application_id IS NOT NULL")
    elif matched is False:
        # A queue of things to fix, not a list of everything without an
        # application. Personal mail correctly has no application and always
        # will, so including it made "unmatched" 63,598 - essentially the whole
        # mailbox - when the population a person can actually act on is 4,458.
        #
        # Three exclusions, each for its own reason:
        #   not_job_related      already right, and 83% of the corpus
        #   unclassified         nothing has looked yet; that is a backlog,
        #                        reachable as classified=false
        #   not_an_application   the matcher refused ON PURPOSE - a recruiter
        #                        approach belongs to no application by design
        #
        # I fixed the third on /admin/mail in #237 and left this endpoint
        # alone, which is how the queue built on it shipped useless.
        where.append(
            "cm.application_id IS NULL "
            "AND ce.kind IS NOT NULL AND ce.kind <> 'not_job_related' "
            "AND COALESCE(cm.method, '') <> %(refused)s"
        )
        params["refused"] = mail_match.NOT_AN_APPLICATION
    predicate = " AND ".join(where)

    base = f"""
        FROM email_messages m
        LEFT JOIN (
            SELECT DISTINCT ON (message_id) message_id, kind, confidence, detail
            FROM email_events ORDER BY message_id, id DESC
        ) ce ON ce.message_id = m.id
        LEFT JOIN (
            SELECT DISTINCT ON (message_id) message_id, application_id, method
            FROM application_matches ORDER BY message_id, id DESC
        ) cm ON cm.message_id = m.id
        LEFT JOIN applications a ON a.id = cm.application_id
        WHERE {predicate}
    """
    total = db.query_one(f"SELECT count(*) AS n {base}", params)
    # A whitelist, not interpolation of whatever arrives: this string is
    # concatenated into SQL, and an unknown value falls back rather than
    # reaching the database.
    order = _USER_MAIL_SORTS.get(sort, _USER_MAIL_SORTS["sent_at"])
    direction = "ASC" if dir == "asc" else "DESC"
    rows = db.query_as(
        MailMessage,
        f"""
        SELECT m.id, m.subject, m.from_email, m.sent_at, m.source,
               ce.kind, ce.confidence,
               ce.detail->>'company' AS extracted_company,
               cm.application_id, cm.method,
               a.company_name, a.title
        {base}
        ORDER BY {order} {direction} NULLS LAST, m.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    # Per-kind counts over the SAME predicate, so a tab's number and its
    # contents cannot disagree. One extra aggregate rather than one request per
    # tab - and without it mail tabs either show no counts or cost a round trip
    # each to display a number the server already had in hand.
    by_kind = {
        r["kind"]: r["n"]
        for r in db.query(f"SELECT ce.kind, count(*) AS n {base} GROUP BY ce.kind", params)
        if r["kind"]
    }
    return UserMail(
        messages=rows,
        total=(total or {}).get("n", 0),
        has_more=offset + len(rows) < (total or {}).get("n", 0),
        by_kind=by_kind,
    )


@router.get("/user/messages/{message_id}/candidates")
def match_candidates(
    message_id: int,
    q: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    user: AuthedUser = Depends(require_user),
) -> Candidates:
    """What this message could belong to, best guesses first.

    The default order is not a search ranking, it is the matcher's own
    reasoning made visible. `_by_company` REFUSES when two applications at one
    employer are both plausible, and those rejected candidates are exactly
    what a person should be shown first - the system already knows the answer
    is one of them and only declined to guess which.

    Board jobs with no application are included because the correction a user
    most often wants is "this belongs to a job I tracked but never recorded
    applying to", and there is nothing to attach to until one exists.
    """
    message = _owned_message(message_id, user.id)
    return _candidates_payload(message, user.id, q, limit)


class MessageAssigned(BaseModel):
    """What the assignment wrote. `messages_assigned` is how many moved: one
    click that quietly reassigns a dozen messages should report it rather than
    have the count discovered later."""

    ok: bool
    application_id: int
    messages_assigned: int


@router.post("/user/messages/{message_id}/assign")
def assign_message(
    message_id: int, body: Assignment, user: AuthedUser = Depends(require_user)
) -> MessageAssigned:
    """Attach this message to an application, creating one if asked.

    Appends. The previous match stays in the log and stops counting by
    latest-wins, so a correction never destroys the evidence for the decision
    it is correcting.

    A job_id creates the application from the tracked posting; a bare company
    and title creates a job-less one, which is the normal shape for mail
    predating the catalog. Never the other way round: an email does not get to
    invent a `jobs` row.
    """
    _owned_message(message_id, user.id)
    application_id = body.application_id

    if application_id is not None:
        owned = db.query_one(
            "SELECT id FROM applications WHERE id = %s AND user_id = %s",
            (application_id, user.id),
        )
        if owned is None:
            raise HTTPException(status_code=404, detail="application not found")
    elif body.job_id is not None:
        job = db.query_one(
            "SELECT j.id, j.company, j.title, uj.date_applied FROM jobs j "
            "JOIN user_jobs uj ON uj.job_id = j.id AND uj.user_id = %s WHERE j.id = %s",
            (user.id, body.job_id),
        )
        if job is None:
            raise HTTPException(status_code=404, detail="job not on your board")
        existing = db.query_one(
            "SELECT id FROM applications WHERE user_id = %s AND job_id = %s",
            (user.id, body.job_id),
        )
        if existing:
            application_id = existing["id"]
        else:
            created = db.query_one(
                "INSERT INTO applications (user_id, job_id, company_name, title, "
                "source_provenance, applied_at) VALUES (%s, %s, %s, %s, 'tracker', %s) "
                "RETURNING id",
                (user.id, job["id"], job["company"], job["title"], job["date_applied"]),
            )
            if created is None:
                raise HTTPException(status_code=500, detail="could not create the application")
            application_id = created["id"]
    elif body.company_name:
        created = db.query_one(
            "INSERT INTO applications (user_id, job_id, company_name, title, "
            "source_provenance, applied_at) VALUES (%s, NULL, %s, %s, 'manual', "
            "(SELECT sent_at FROM email_messages WHERE id = %s)) RETURNING id",
            (user.id, body.company_name, body.title, message_id),
        )
        if created is None:
            raise HTTPException(status_code=500, detail="could not create the application")
        application_id = created["id"]
    else:
        raise HTTPException(
            status_code=400,
            detail="give an application_id, a job_id, or a company_name to create one",
        )

    # The provider's own thread id, never a derived one. Grouping threadless
    # mail by normalised subject and sender was measured and is unsafe: "thank
    # you for applying!" from myworkday.com is 49 messages from 49 DIFFERENT
    # employers, and merging those would attach 49 unrelated applications to
    # one. The correct signal is the References/In-Reply-To chain, which this
    # importer discards - `headers` is empty on all 67k rows - so until that is
    # fixed, threadless mail is assigned one message at a time.
    targets = [message_id]
    if body.whole_thread:
        siblings = db.query(
            f"""
            SELECT m.id FROM email_messages m
            WHERE m.user_id = %(user)s
              AND {_THREAD_KEY} = (
                  SELECT {_THREAD_KEY} FROM email_messages m WHERE m.id = %(msg)s
              )
              AND m.id <> %(msg)s
            ORDER BY m.id
            LIMIT %(cap)s
            """,
            {"user": user.id, "msg": message_id, "cap": MAX_THREAD_FANOUT},
        )
        targets.extend(r["id"] for r in siblings)

    rationale = body.note or "assigned by the user"
    for index, target in enumerate(targets):
        mail_match.record(
            target,
            mail_match.Match(
                application_id,
                mail_match.MANUAL,
                "high",
                rationale if index == 0 else f"{rationale} (same conversation)",
            ),
            actor_user_id=user.id,
        )
    mail_pipeline.sync_action_items(application_id)
    return MessageAssigned(ok=True, application_id=application_id, messages_assigned=len(targets))


class MailEvent(BaseModel):
    """One classification of this message, as written. The log is append-only
    and the newest wins, so a message with three of these was corrected twice.
    A null `model` is how a human correction is told apart from a model's."""

    id: int
    kind: str
    confidence: str | None
    occurred_at: datetime.datetime | None
    deadline_at: datetime.datetime | None
    deadline_inferred: bool
    detail: dict[str, Any] | None
    model: str | None
    created_at: datetime.datetime


class MailMatchRow(BaseModel):
    """One attempt to place this message, with the application it named."""

    id: int
    application_id: int | None
    method: str
    confidence: str | None
    rationale: str | None
    created_at: datetime.datetime
    company_name: str | None
    title: str | None


class ReadableMessage(BaseModel):
    """A message as it can be shown to the person it belongs to.

    `body_html` is SANITISED on read and the stored markup never leaves the
    server: a caller holding the raw markup will eventually render it, and the
    sandboxed iframe on the other side is only the second of two layers.
    `blocked_remote_content` says what was withheld, so a reader offering
    "load images" knows whether there is anything to load."""

    id: int
    subject: str | None
    from_email: str | None
    sent_at: datetime.datetime | None
    source: str
    body_text: str | None
    body_html: str | None
    blocked_remote_content: int


class MailMessageDetail(ReadableMessage):
    """The whole message with its history, for when the excerpt is not enough.

    Read-only and user-scoped. The body is already stored - withholding it
    would mean leaving for a mail client to check a decision this system
    made, which is the same as not being able to check it."""

    provider_message_id: str
    provider_thread_id: str | None
    from_name: str | None
    to_emails: list[str] | None
    prefilter_hit: bool | None
    prefilter_reason: str | None
    events: list[MailEvent]
    matches: list[MailMatchRow]


@router.get("/user/messages/{message_id}")
def message_detail(message_id: int, user: AuthedUser = Depends(require_user)) -> MailMessageDetail:
    """The whole message, for when the excerpt is not enough.

    Read-only and user-scoped. The body is already stored - withholding it
    would mean a person has to leave for their mail client to check a decision
    this system made, which is the same as not being able to check it.
    """
    row = db.query_one(
        "SELECT id, provider_message_id, provider_thread_id, source, from_email, from_name, "
        "to_emails, subject, sent_at, body_text, body_html, prefilter_hit, prefilter_reason "
        "FROM email_messages WHERE id = %s AND user_id = %s",
        (message_id, user.id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="message not found")
    # Sanitised on READ, never stored sanitised: body_html stays the message as
    # it arrived, so a better sanitiser improves every message ever received
    # rather than only the ones that come next. The raw markup is deliberately
    # NOT returned - a caller that has it will eventually render it.
    return MailMessageDetail(
        **_readable(row),
        events=db.query_as(
            MailEvent,
            "SELECT id, kind, confidence, occurred_at, deadline_at, deadline_inferred, detail, "
            "model, created_at FROM email_events WHERE message_id = %s ORDER BY id",
            (message_id,),
        ),
        matches=db.query_as(
            MailMatchRow,
            """
            SELECT am.id, am.application_id, am.method, am.confidence, am.rationale,
                   am.created_at, a.company_name, a.title
            FROM application_matches am
            LEFT JOIN applications a ON a.id = am.application_id
            WHERE am.message_id = %s ORDER BY am.id
            """,
            (message_id,),
        ),
    )


@router.post("/user/messages/{message_id}/classify")
def correct_classification(
    message_id: int, body: Reclassification, user: AuthedUser = Depends(require_user)
) -> Reclassified:
    """Say what a message actually is, when the classifier got it wrong.

    Every other correction here fixes the MATCH. Nothing fixed the kind - and
    stage is derived from kinds, so a rejection read as an acknowledgement
    silently moves an application and the only affordance on offer was
    detaching a match that was correct.

    Appends an event rather than editing one. Events are append-only and the
    latest per message wins, so this supersedes the model's answer by the same
    rule a re-classification does, and the wrong answer stays visible in the
    history. model is NULL, which is how a human correction is told apart from
    a model's: nothing else writes an event without one.

    The stage recomputes on read, so the correction propagates with nobody
    restating it - the same property that makes detaching work.
    """
    message = _owned_message(message_id, user.id)
    return _apply_classification(message, body, actor_user_id=user.id)


class MessageKinds(BaseModel):
    kinds: list[str]


@router.get("/user/message-kinds")
def message_kinds(user: AuthedUser = Depends(require_user)) -> MessageKinds:
    """The vocabulary, served rather than copied.

    The client kept this list in two places and it drifts the moment a kind is
    added - the same failure as the stage vocabulary, which had a terminal
    state the frontend did not know about.
    """
    return MessageKinds(kinds=sorted(EVENT_KINDS))


@router.post("/user/messages/{message_id}/classify/revert", response_model_exclude_none=True)
def revert_classification(message_id: int, user: AuthedUser = Depends(require_user)) -> Reverted:
    """Undo a correction by restoring what the model last said.

    Another append, not a delete: a mis-correction has to be recoverable and
    the log still has to show that both happened. Refused when the model has
    never classified this message, because there is nothing to restore.
    """
    _owned_message(message_id, user.id)
    return _apply_revert(message_id, actor_user_id=user.id)


def _readable(row: dict[str, Any]) -> dict[str, Any]:
    """Swap stored markup for display-safe markup, and say what was withheld.

    The raw html never leaves the server: a caller holding it will eventually
    render it, and the sandboxed iframe on the other side is only the second
    of two layers.
    """
    raw = row.pop("body_html", None)
    safe, blocked = sanitise(raw) if raw else (None, 0)
    return {**row, "body_html": safe, "blocked_remote_content": blocked}


class ThreadMessage(ReadableMessage):
    """One message of a conversation, with what the pipeline made of it.

    Sanitised per message, same as the single-message reader: a thread is
    where a person reads mail in context, so serving it as stripped text here
    and as rendered mail one click away would be the same message in two
    shapes."""

    from_name: str | None
    kind: str | None
    confidence: str | None
    extracted_company: str | None
    application_id: int | None
    method: str | None
    company_name: str | None
    title: str | None


class Thread(BaseModel):
    """The conversation, and whether it is all of it. A thread silently cut at
    the cap reads as a conversation that ended, so the cut is said."""

    messages: list[ThreadMessage]
    total: int
    truncated: bool


@router.get("/user/messages/{message_id}/thread")
def read_thread(
    message_id: int,
    limit: int = Query(default=MAX_THREAD_FANOUT, ge=1, le=200),
    user: AuthedUser = Depends(require_user),
) -> Thread:
    """The conversation this message belongs to, oldest first.

    Mail is a flat list of messages and the unit a person thinks in is the
    exchange: 19,995 messages sit in 4,141 conversations of more than one, so
    roughly a third of the corpus is currently shown out of its context.

    Keyed on COALESCE(thread id, own message id), the same derivation #235
    introduced - the provider's thread id is the first References entry, which
    every reply carries and the message that STARTED the thread does not. Left
    alone it excludes the original from its own conversation.

    Capped and honest about it. The longest key in this corpus holds 474
    messages, which is a mailing list reusing a thread id rather than a
    conversation, and a reader who asked for a thread should not be handed one.
    """
    _owned_message(message_id, user.id)
    rows = db.query(
        f"""
        WITH key AS (
            SELECT {_THREAD_KEY} AS k FROM email_messages m WHERE m.id = %(msg)s
        ),
        current_event AS (
            SELECT DISTINCT ON (message_id) message_id, kind, confidence, detail
            FROM email_events ORDER BY message_id, id DESC
        ),
        current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id, method
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT m.id, m.subject, m.from_email, m.from_name, m.sent_at, m.source,
               m.body_text, m.body_html, ce.kind, ce.confidence,
               ce.detail->>'company' AS extracted_company,
               cm.application_id, cm.method,
               a.company_name, a.title
        FROM email_messages m
        LEFT JOIN current_event ce ON ce.message_id = m.id
        LEFT JOIN current_match cm ON cm.message_id = m.id
        LEFT JOIN applications a ON a.id = cm.application_id
        WHERE m.user_id = %(user)s AND {_THREAD_KEY} = (SELECT k FROM key)
        ORDER BY m.sent_at, m.id
        LIMIT %(limit)s
        """,
        {"user": user.id, "msg": message_id, "limit": limit + 1},
    )
    truncated = len(rows) > limit
    return Thread(
        # 0.7ms per message measured on real bodies, so a full 40-message
        # thread costs about 28ms to sanitise.
        messages=[ThreadMessage(**_readable(row)) for row in rows[:limit]],
        total=len(rows[:limit]),
        truncated=truncated,
    )


# Aggregates, so they are the ORDER BY expressions rather than column names.
# started_at ascending is "oldest conversation first", which is how a backlog
# is worked; message_count descending finds the ones that have been going on.
_THREAD_SORTS = {
    "last_activity_at": "max(m.sent_at)",
    "started_at": "min(m.sent_at)",
    "message_count": "count(*)",
}


class ThreadSummary(BaseModel):
    """A conversation as a row: when it ran, who was in it, and whether it
    still needs somebody.

    `needs_attention` is the correctable pile - a thread carrying job-related
    mail that reached no application. Not "unread": there is no such concept
    here and inventing one would be a second inbox to maintain."""

    thread_id: str
    message_count: int
    last_activity_at: datetime.datetime | None
    started_at: datetime.datetime | None
    subject: str | None
    # Null rather than empty when every message in the thread lacks one, which
    # is what the FILTER on the aggregate produces.
    participants: list[str] | None
    kinds: list[str] | None
    needs_attention: bool
    latest_message_id: int
    application_id: int | None


class Threads(BaseModel):
    threads: list[ThreadSummary]
    total: int
    has_more: bool


@router.get("/user/threads")
def list_threads(
    needs_attention: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    sort: str = Query(default="last_activity_at"),
    dir: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
) -> Threads:
    """Conversations, newest activity first.

    The list form of a thread rather than a list of messages. Grouping a page
    of messages client-side would produce partial threads at the page
    boundaries - a conversation cut in half by pagination reads as a
    conversation that is half that long, which is a lie exactly where nobody
    looks for one.

    Keyed on COALESCE(thread id, own message id), the same derivation the
    thread reader uses, so a conversation contains the message that started it.

    needs_attention is the correctable pile: a thread carrying job-related mail
    that reached no application. Not "unread" - we have no such concept and
    inventing one would be a second inbox to maintain.
    """
    where = ["m.user_id = %(user)s"]
    params: dict[str, Any] = {"user": user.id, "limit": limit, "offset": offset}
    if q:
        where.append("(m.subject ILIKE %(q)s OR m.from_email ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    predicate = " AND ".join(where)

    # One predicate, negated rather than written twice: two spellings of the
    # same condition is how a filter and its inverse stop being complements.
    _CORRECTABLE = (
        "bool_or(ce.kind IS NOT NULL AND ce.kind <> 'not_job_related' "
        "AND cm.application_id IS NULL)"
    )
    having = ""
    if needs_attention is True:
        having = f"HAVING {_CORRECTABLE}"
    elif needs_attention is False:
        having = f"HAVING NOT {_CORRECTABLE}"

    base = f"""
        FROM email_messages m
        LEFT JOIN (
            SELECT DISTINCT ON (message_id) message_id, kind FROM email_events
            ORDER BY message_id, id DESC
        ) ce ON ce.message_id = m.id
        LEFT JOIN (
            SELECT DISTINCT ON (message_id) message_id, application_id FROM application_matches
            ORDER BY message_id, id DESC
        ) cm ON cm.message_id = m.id
        WHERE {predicate}
        GROUP BY {_THREAD_KEY}
        {having}
    """
    total = db.query_one(f"SELECT count(*) AS n FROM (SELECT 1 {base}) s", params)
    # Whitelisted, because these are aggregates concatenated into SQL and an
    # unknown value must fall back rather than reach the database.
    order = _THREAD_SORTS.get(sort, _THREAD_SORTS["last_activity_at"])
    direction = "ASC" if dir == "asc" else "DESC"
    rows = db.query_as(
        ThreadSummary,
        f"""
        SELECT {_THREAD_KEY} AS thread_id,
               count(*) AS message_count,
               max(m.sent_at) AS last_activity_at,
               min(m.sent_at) AS started_at,
               (array_agg(m.subject ORDER BY m.sent_at) FILTER (WHERE m.subject IS NOT NULL))[1]
                   AS subject,
               array_agg(DISTINCT m.from_email) FILTER (WHERE m.from_email IS NOT NULL)
                   AS participants,
               array_agg(DISTINCT ce.kind) FILTER (WHERE ce.kind IS NOT NULL) AS kinds,
               count(*) FILTER (
                   WHERE ce.kind IS NOT NULL AND ce.kind <> 'not_job_related'
                     AND cm.application_id IS NULL
               ) > 0 AS needs_attention,
               (array_agg(m.id ORDER BY m.sent_at DESC))[1] AS latest_message_id,
               max(cm.application_id) AS application_id
        {base}
        ORDER BY {order} {direction} NULLS LAST
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    return Threads(
        threads=rows,
        total=(total or {}).get("n", 0),
        has_more=offset + len(rows) < (total or {}).get("n", 0),
    )
