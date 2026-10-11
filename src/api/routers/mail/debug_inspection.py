"""Inspect current mail verdicts and full append-only histories."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api import db, pagination, scoping, sorting
from api import params as params_
from api.auth import AuthedUser
from api.mail import match as mail_match
from api.problem import refuse
from api.routers.admin import require_admin
from api.routers.mail.debug_models import (
    AdminMailDetail,
    AdminMailEvent,
    AdminMailMatch,
    AdminMailMessage,
    MailList,
    MailRow,
)

list_router = APIRouter()
detail_router = APIRouter()

_MAIL_SORT_COLUMNS = {
    "sent_at": "m.sent_at",
    "imported_at": "m.imported_at",
    "id": "m.id",
}

# The wire name for "no match row at all", which is a third state and not an
# absence. `unmatched` means the matcher ran and found nothing;
# `not_an_application` means it correctly refused to look; NEVER_ATTEMPTED
# means nothing has run yet. The first two look identical in any aggregate and
# mean opposite things, and the third is the one worth filtering for when
# hunting failures - so it needs a name rather than being reachable only as a
# gap in a list.
NEVER_ATTEMPTED = "never_attempted"


def _mail_filters(
    *,
    kind: str | list[str] | None,
    matched: bool | None,
    source: str | list[str] | None,
    prefilter: bool | None,
    q: str | None,
    method: str | list[str] | None = None,
    job_related: bool | None = None,
    classified: bool | None = None,
    user_ids: list[int] | None = None,
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    kinds, sources, methods = params_.csv(kind), params_.csv(source), params_.csv(method)
    if user_ids:
        clauses.append(scoping.column("m.user_id"))
        params["user_ids"] = user_ids
    if kinds:
        clauses.append("ev.kind = ANY(%(kind)s)")
        params["kind"] = kinds
    if matched is not None:
        # NULL application_id is a real recorded outcome - "we looked and
        # found nothing" - so unmatched is a value to filter on, not an
        # absence to skip over.
        #
        # But matched=false must NOT sweep in the deliberate refusals. A
        # recruiter approach belongs to no application by design, so counting
        # it as a matching failure turns correct behaviour into a defect on
        # screen - and it is 1,374 rows, which would swamp the real failures
        # in the pile a person goes to when hunting them.
        if matched:
            clauses.append("mt.application_id IS NOT NULL")
        else:
            clauses.append("mt.application_id IS NULL AND COALESCE(mt.method, '') <> %(refused)s")
            params["refused"] = mail_match.NOT_AN_APPLICATION
    if classified is not None:
        # THREE states, not two. "Not job related" and "nothing has looked at
        # it yet" are different facts and I collapsed them - the same mistake
        # as unmatched versus not_an_application, made in the filter written to
        # fix that one. e3 found it from the frontend: the prefilter matrix
        # inner-joins email_events and so counts only CLASSIFIED messages,
        # while job_related=false also matched the 16,182 unclassified, so the
        # cell linked to a larger set than the number it displayed.
        clauses.append("ev.kind IS NOT NULL" if classified else "ev.kind IS NULL")
    if job_related is not None:
        # `kind` is an equality filter, so "job-related" - the whole point of
        # the prefilter matrix - was not expressible: a person could open
        # "prefilter did not match" and not "job-related AND prefilter did not
        # match", which IS the 2,244 messages a gate would have dropped. That
        # is the number the gate decision rests on.
        #
        # NULL kind is not job-related here: nothing has classified it, so it
        # cannot be evidence either way.
        # Both branches now require a classification, so job_related is a
        # statement about what the classifier SAID rather than about whether it
        # has spoken. Ask `classified=false` for the backlog.
        clauses.append(
            "ev.kind IS NOT NULL AND ev.kind <> 'not_job_related'"
            if job_related
            else "ev.kind = 'not_job_related'"
        )
    if methods:
        # never_attempted is an absence, not a method value, so it is its own
        # branch inside the same OR as the named methods.
        parts = []
        if NEVER_ATTEMPTED in methods:
            parts.append("mt.match_id IS NULL")
        named = [x for x in methods if x != NEVER_ATTEMPTED]
        if named:
            parts.append("mt.method = ANY(%(method)s)")
            params["method"] = named
        clauses.append("(" + " OR ".join(parts) + ")")
    if sources:
        clauses.append("m.source = ANY(%(source)s)")
        params["source"] = sources
    if prefilter is not None:
        clauses.append("COALESCE(m.prefilter_hit, FALSE) = %(prefilter)s")
        params["prefilter"] = prefilter
    if q:
        clauses.append("(m.subject ILIKE %(q)s OR m.from_email ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


# Latest row per message for both, because both logs are append-only and only
# the newest verdict counts. Spelled once here so the list and the detail view
# cannot disagree about what "current" means.
_CURRENT_VERDICTS = """
    LEFT JOIN LATERAL (
        SELECT kind, confidence, deadline_at, deadline_inferred, detail, model, id
        FROM email_events WHERE message_id = m.id ORDER BY id DESC LIMIT 1
    ) ev ON TRUE
    LEFT JOIN LATERAL (
        SELECT application_id, method, confidence AS match_confidence, rationale, id AS match_id
        FROM application_matches WHERE message_id = m.id ORDER BY id DESC LIMIT 1
    ) mt ON TRUE
"""


@list_router.get("/admin/mail")
def list_mail(
    kind: str | None = None,
    matched: bool | None = None,
    source: str | None = None,
    prefilter: bool | None = None,
    method: str | None = None,
    job_related: bool | None = None,
    classified: bool | None = None,
    q: str | None = None,
    users: str | None = Query(default=None, alias="user"),
    sort: str = "sent_at",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    user: AuthedUser = Depends(require_admin),
) -> MailList:
    ids = scoping.user_ids(users)
    where, params = _mail_filters(
        kind=kind,
        matched=matched,
        source=source,
        prefilter=prefilter,
        q=q,
        method=method,
        job_related=job_related,
        classified=classified,
        user_ids=ids,
    )
    paging = pagination.Page.from_params(page, page_size, maximum=200)
    total = db.query_one(
        f"SELECT COUNT(*) AS c FROM email_messages m {_CURRENT_VERDICTS} WHERE TRUE {where}", params
    )
    sorts = sorting.parse(sort, dir, _MAIL_SORT_COLUMNS, "sent_at")
    rows = db.query_as(
        MailRow,
        f"""
        SELECT m.id, m.provider_message_id, m.source, m.from_email, m.subject, m.sent_at,
               m.prefilter_hit, m.prefilter_reason,
               ev.kind, ev.confidence, ev.deadline_at, ev.deadline_inferred, ev.model,
               mt.application_id, mt.method, mt.match_confidence, mt.rationale,
               a.company_name, a.title
        FROM email_messages m
        {_CURRENT_VERDICTS}
        LEFT JOIN applications a ON a.id = mt.application_id
        WHERE TRUE {where}
        ORDER BY {sorting.clause(sorts, _MAIL_SORT_COLUMNS)}, m.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {**params, "limit": paging.size, "offset": paging.offset},
    )
    n = total["c"] if total else 0
    return MailList(
        sorts=sorts,
        sortable=sorted(_MAIL_SORT_COLUMNS),
        rows=rows,
        **paging.metadata(n),
        filters=params_.applied(
            kind=params_.csv(kind),
            method=params_.csv(method),
            source=params_.csv(source),
            user=scoping.echo(ids),
        ),
        filterable=[
            "kind",
            "matched",
            "source",
            "prefilter",
            "method",
            "job_related",
            "classified",
            "q",
            "user",
        ],
    )


@detail_router.get("/admin/mail/{message_id}")
def mail_detail(message_id: int, user: AuthedUser = Depends(require_admin)) -> AdminMailDetail:
    """One message with its FULL history, not just the current verdict.

    Every classification and every match attempt, oldest first. That history
    is the point: a match that changed when a posting finally reached the
    board, or a classification corrected on a later pass, is exactly what
    someone debugging a wrong answer needs to see.
    """
    message = db.query_one_as(
        AdminMailMessage,
        "SELECT id, user_id, provider_message_id, provider_thread_id, thread_topic, source, "
        "from_email, from_name, to_emails, subject, sent_at, body_text, body_html, headers, "
        "prefilter_hit, prefilter_reason, imported_at FROM email_messages WHERE id = %s",
        (message_id,),
    )
    if not message:
        raise refuse(404, "NOT_FOUND", "unknown message")
    return AdminMailDetail(
        message=message,
        events=db.query_as(
            AdminMailEvent,
            "SELECT id, message_id, kind, confidence, occurred_at, deadline_at, "
            "deadline_inferred, detail, model, actor_user_id, created_at "
            "FROM email_events WHERE message_id = %s ORDER BY id",
            (message_id,),
        ),
        matches=db.query_as(
            AdminMailMatch,
            """
            SELECT am.id, am.message_id, am.application_id, am.method, am.confidence,
                   am.rationale, am.actor_user_id, am.created_at,
                   a.company_name, a.title, a.job_id
            FROM application_matches am
            LEFT JOIN applications a ON a.id = am.application_id
            WHERE am.message_id = %s ORDER BY am.id
            """,
            (message_id,),
        ),
        canonical_urls=sorted(mail_match.canonical_urls(message.body_text)),
    )
