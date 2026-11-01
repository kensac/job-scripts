"""Provider-keyed conversations in a person's mailbox."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.mail.current import current_event, current_match
from api.mail.store import owned_message
from api.routers.mail.conversation_identity import CONVERSATION_KEY_SQL, MAX_THREAD_FANOUT
from api.routers.mail.message_detail import ReadableMessage, readable_message_fields

router = APIRouter()


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
    owned_message(message_id, user.id)
    rows = db.query(
        f"""
        WITH key AS (
            SELECT {CONVERSATION_KEY_SQL} AS k FROM email_messages m WHERE m.id = %(msg)s
        ),
        current_event AS (
            {current_event("kind", "confidence", "detail")}
        ),
        current_match AS (
            {current_match("application_id", "method")}
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
        WHERE m.user_id = %(user)s AND {CONVERSATION_KEY_SQL} = (SELECT k FROM key)
        ORDER BY m.sent_at, m.id
        LIMIT %(limit)s
        """,
        {"user": user.id, "msg": message_id, "limit": limit + 1},
    )
    truncated = len(rows) > limit
    return Thread(
        # 0.7ms per message measured on real bodies, so a full 40-message
        # thread costs about 28ms to sanitise.
        messages=[ThreadMessage(**readable_message_fields(row)) for row in rows[:limit]],
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
            {current_event("kind")}
        ) ce ON ce.message_id = m.id
        LEFT JOIN (
            {current_match("application_id")}
        ) cm ON cm.message_id = m.id
        WHERE {predicate}
        GROUP BY {CONVERSATION_KEY_SQL}
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
        SELECT {CONVERSATION_KEY_SQL} AS thread_id,
               count(*) AS message_count,
               max(m.sent_at) AS last_activity_at,
               min(m.sent_at) AS started_at,
               (array_agg(m.subject ORDER BY m.sent_at) FILTER (WHERE m.subject IS NOT NULL))[1]
                   AS subject,
               array_agg(DISTINCT m.from_email) FILTER (WHERE m.from_email IS NOT NULL)
                   AS participants,
               array_agg(DISTINCT ce.kind) FILTER (WHERE ce.kind IS NOT NULL) AS kinds,
               {_CORRECTABLE} AS needs_attention,
               (array_agg(m.id ORDER BY m.sent_at DESC))[1] AS latest_message_id,
               max(cm.application_id) AS application_id
        {base}
        ORDER BY {order} {direction} NULLS LAST, thread_id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    return Threads(
        threads=rows,
        total=(total or {}).get("n", 0),
        has_more=offset + len(rows) < (total or {}).get("n", 0),
    )
