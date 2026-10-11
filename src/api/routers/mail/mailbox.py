"""A person's own mail list and its per-kind counts."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db
from api import params as params_
from api.auth import AuthedUser, require_user
from api.mail import match as mail_match
from api.mail.current import current_event, current_match

router = APIRouter()

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
        kinds = params_.csv(kind)
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
            {current_event("kind", "confidence", "detail")}
        ) ce ON ce.message_id = m.id
        LEFT JOIN (
            {current_match("application_id", "method")}
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
