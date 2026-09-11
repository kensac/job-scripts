"""Everybody's mail, for an administrator hunting a wrong answer.

The debug view: it exists to answer "why did the classifier decide that", it
spans every user, and it is gated behind infra-admin. The distributions sit
here too, because finding a pattern by reading a hundred messages is work a
GROUP BY does in a second, and so do the corrections an administrator makes to
somebody else's mailbox.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import db, pagination, rates, scoping, sorting
from api import params as params_
from api.auth import AuthedUser
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.mail.match import CurrentMatch
from api.rates import Rate
from api.routers.admin import require_admin
from api.routers.mail.shared import (
    Candidates,
    Reclassification,
    Reclassified,
    Reverted,
    _apply_classification,
    _apply_revert,
    _candidates_payload,
)

router = APIRouter()


_SORTABLE = {
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


def _where(
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
_CURRENT = """
    LEFT JOIN LATERAL (
        SELECT kind, confidence, deadline_at, deadline_inferred, detail, model, id
        FROM email_events WHERE message_id = m.id ORDER BY id DESC LIMIT 1
    ) ev ON TRUE
    LEFT JOIN LATERAL (
        SELECT application_id, method, confidence AS match_confidence, rationale, id AS match_id
        FROM application_matches WHERE message_id = m.id ORDER BY id DESC LIMIT 1
    ) mt ON TRUE
"""


class MailRow(BaseModel):
    """One message as the debug list shows it: what arrived, what the
    classifier last said, and what the matcher last did with it.

    Every field after `prefilter_reason` is the CURRENT row of an append-only
    log and can be null for its own reason - no classification yet, no match
    attempted, a match that found nothing. They are not interchangeable and
    `_where` filters on the difference."""

    id: int
    provider_message_id: str
    source: str
    from_email: str | None
    subject: str | None
    sent_at: datetime.datetime | None
    prefilter_hit: bool | None
    prefilter_reason: str | None
    kind: str | None
    confidence: str | None
    deadline_at: datetime.datetime | None
    deadline_inferred: bool | None
    model: str | None
    application_id: int | None
    method: str | None
    match_confidence: str | None
    rationale: str | None
    company_name: str | None
    title: str | None


class MailList(BaseModel):
    """The page, plus what it was sorted and filtered by.

    `sortable` and `filterable` are the vocabulary a client builds its controls
    from, and `sorts`/`filters` echo what was actually applied, so the page
    never has to duplicate the default or guess the keys."""

    sorts: list[dict[str, str]]
    sortable: list[str]
    rows: list[MailRow]
    page: int
    page_size: int
    total: int
    has_more: bool
    filters: dict[str, list[str]]
    filterable: list[str]


@router.get("/admin/mail")
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
    where, params = _where(
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
        f"SELECT COUNT(*) AS c FROM email_messages m {_CURRENT} WHERE TRUE {where}", params
    )
    sorts = sorting.parse(sort, dir, _SORTABLE, "sent_at")
    rows = db.query_as(
        MailRow,
        f"""
        SELECT m.id, m.provider_message_id, m.source, m.from_email, m.subject, m.sent_at,
               m.prefilter_hit, m.prefilter_reason,
               ev.kind, ev.confidence, ev.deadline_at, ev.deadline_inferred, ev.model,
               mt.application_id, mt.method, mt.match_confidence, mt.rationale,
               a.company_name, a.title
        FROM email_messages m
        {_CURRENT}
        LEFT JOIN applications a ON a.id = mt.application_id
        WHERE TRUE {where}
        ORDER BY {sorting.clause(sorts, _SORTABLE)}, m.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {**params, "limit": paging.size, "offset": paging.offset},
    )
    n = total["c"] if total else 0
    return MailList(
        sorts=sorts,
        sortable=sorted(_SORTABLE),
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


# Identical input must not get different labels. That is checkable forever
# without anyone hand-labelling anything, which is what makes it a regression
# metric rather than an audit: it needs no ground truth, only agreement with
# itself.
#
# GROUPED BY BODY, NOT BY SUBJECT. Grouping on sender+subject reports 6.6% of
# groups inconsistent, and the worst offenders are all "Re:" threads - a
# conversation is many different messages sharing a subject, and its parts
# SHOULD get different kinds. Hashing the body measures what the claim
# actually is. Digits are normalised out first so that a template differing
# only by a reference number still counts as the same input.
#
# Two copies is the floor because you need two to disagree, and the floor is
# not load-bearing: the rate is 1.7% at two, 2.2% at three, 2.9% at ten. A
# number that barely moves across the range is one nobody needs to tune.
_CONSISTENCY_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (message_id) message_id, kind
    FROM email_events ORDER BY message_id, id DESC
),
groups AS (
    SELECT lower(m.from_email) AS sender,
           md5(regexp_replace(m.body_text, '[0-9]+', '#', 'g')) AS body_hash,
           count(*) AS copies,
           count(DISTINCT l.kind) AS kinds,
           array_agg(DISTINCT l.kind) AS kind_list,
           (array_agg(m.subject ORDER BY m.id))[1] AS subject,
           (array_agg(m.id ORDER BY m.id))[1] AS example_message_id
    FROM email_messages m
    JOIN latest l ON l.message_id = m.id
    WHERE m.body_text IS NOT NULL AND length(m.body_text) > %(min_len)s
    GROUP BY 1, 2
    HAVING count(*) >= 2
)
SELECT sender, subject, copies, kinds, kind_list, example_message_id
FROM groups ORDER BY kinds DESC, copies DESC
"""

# Below this a body is a stub - a bare signature, a one-line auto-reply - and
# thousands of them hash together into a group that means nothing.
_CONSISTENCY_MIN_BODY = 200


class ConsistencyGroup(BaseModel):
    """One body the classifier saw more than once, and the answers it gave."""

    sender: str | None
    subject: str | None
    copies: int
    kinds: list[str]
    example_message_id: int


class ConsistencyCoverage(BaseModel):
    """What the rate is a rate OF. Only a repeated body can be checked this
    way, so this is a canary over part of the corpus and never a measure of
    it - a rate quoted without this would describe a canary as a census."""

    messages_covered: int
    messages_classified: int
    min_copies: int
    min_body_chars: int


class ClassificationConsistency(BaseModel):
    groups: Rate
    messages: Rate
    coverage: ConsistencyCoverage
    worst: list[ConsistencyGroup]


@router.get("/admin/mail/consistency")
def classification_consistency(
    limit: int = Query(default=25, ge=1, le=200),
    user: AuthedUser = Depends(require_admin),
) -> ClassificationConsistency:
    """Where the classifier gave identical inputs different answers.

    A label-free regression metric: no ground truth, only self-agreement, so
    it stays checkable after the corpus changes and after the model does.

    IT COVERS A MINORITY OF THE CORPUS and says so rather than being read as
    an accuracy figure - only messages whose body repeats can be checked this
    way, and that is a fraction of the whole. `messages_covered` against
    `messages_classified` is the denominator; a rate quoted without it would
    be describing a canary as if it were a census.
    """
    rows = db.query(_CONSISTENCY_SQL, {"min_len": _CONSISTENCY_MIN_BODY})
    inconsistent = [r for r in rows if r["kinds"] > 1]
    covered = sum(r["copies"] for r in rows)
    total_row = db.query_one(
        """
        WITH latest AS (
            SELECT DISTINCT ON (message_id) message_id FROM email_events
            ORDER BY message_id, id DESC
        )
        SELECT count(*) AS c FROM latest
        """
    )
    return ClassificationConsistency(
        groups=rates.rate(len(inconsistent), len(rows), rates.DEFAULT_MIN_SAMPLE),
        messages=rates.rate(
            sum(r["copies"] for r in inconsistent), covered, rates.DEFAULT_MIN_SAMPLE
        ),
        coverage=ConsistencyCoverage(
            messages_covered=covered,
            messages_classified=int((total_row or {}).get("c", 0)),
            min_copies=2,
            min_body_chars=_CONSISTENCY_MIN_BODY,
        ),
        worst=[
            ConsistencyGroup(
                sender=r["sender"],
                subject=r["subject"],
                copies=r["copies"],
                kinds=sorted(r["kind_list"]),
                example_message_id=r["example_message_id"],
            )
            for r in inconsistent[:limit]
        ],
    )


class ClassificationCell(BaseModel):
    """What one model said, at one confidence, about how many messages - with
    the two things that go wrong inside a correct-looking answer: no company
    extracted, and a deadline the model inferred rather than read."""

    kind: str
    model: str | None
    confidence: str | None
    messages: int
    no_company: int
    inferred_deadlines: int


class MatchingCell(BaseModel):
    """How many messages of a kind the matcher settled, and by which tier.
    `never attempted` is a method value here because an absence in a
    distribution is a gap nobody can read."""

    kind: str
    method: str
    messages: int


class PrefilterCell(BaseModel):
    prefilter_hit: bool
    job_related: bool
    messages: int


class SenderDomainRow(BaseModel):
    domain: str | None
    messages: int
    matched: int


class SourceRow(BaseModel):
    source: str
    messages: int


class MailCorpus(BaseModel):
    """The whole import, deliberately NOT windowed: how much mail exists, when
    it starts, and how much nothing has looked at are properties of the
    import rather than of a slice."""

    messages: int
    unclassified: int
    oldest: datetime.datetime | None
    newest: datetime.datetime | None
    by_source: list[SourceRow]


class Population(BaseModel):
    """The denominator a section counts over, and what it leaves out. Each
    section below counts a DIFFERENT population and nothing in the rows says
    so, which invites a subtraction that means nothing."""

    messages: int
    excludes: list[str]


class DomainPopulation(Population):
    """The sender breakdown is also TRUNCATED, so it says how many domains it
    shows against how many exist."""

    domains_shown: int
    domains_total: int


class Populations(BaseModel):
    classification: Population
    matching: Population
    prefilter: Population
    sender_domains: DomainPopulation


class PrefilterSummary(BaseModel):
    """A rate the prefilter could never report about itself, and the reason it
    was kept as a signal rather than deleted: the mail a gate WOULD have
    dropped is the one unrecoverable failure."""

    cells: list[PrefilterCell]
    job_related_a_gate_would_have_dropped: int
    job_related_total: int


class MailAnalytics(BaseModel):
    window_days: int | None
    corpus: MailCorpus
    populations: Populations
    classification: list[ClassificationCell]
    matching: list[MatchingCell]
    prefilter: PrefilterSummary
    sender_domains: list[SenderDomainRow]


@router.get("/admin/mail/analytics")
def mail_analytics(
    days: int = Query(default=0, ge=0, le=3650),
    user: AuthedUser = Depends(require_admin),
) -> MailAnalytics:
    """Where the pipeline is wrong, as distributions rather than examples.

    /admin/mail answers "why did THIS message get that answer" and has to show
    the message to do it. That is the wrong tool for finding a pattern: reading
    a hundred messages to notice that one model is producing low-confidence
    answers is work a GROUP BY does in a second.

    days=0 means the whole corpus, which is the right default here. This is a
    historical import spanning 2018 to now, so a 30-day window would describe
    the tail of an eight-year backfill rather than the backfill.
    """
    window = "AND m.sent_at >= now() - make_interval(days => %(days)s)" if days else ""
    params: dict[str, Any] = {"days": days} if days else {}

    def q[Row](row: type[Row], sql: str) -> list[Row]:
        return db.query_as(row, sql.format(window=window), params)

    classification = q(
        ClassificationCell,
        """
        WITH ce AS (
            SELECT DISTINCT ON (message_id) message_id, kind, confidence, model, deadline_inferred,
                   detail
            FROM email_events ORDER BY message_id, id DESC
        )
        SELECT ce.kind, ce.model, ce.confidence,
               count(*) AS messages,
               count(*) FILTER (WHERE ce.detail->>'company' IS NULL) AS no_company,
               count(*) FILTER (WHERE ce.deadline_inferred) AS inferred_deadlines
        FROM email_messages m JOIN ce ON ce.message_id = m.id
        WHERE TRUE {window}
        GROUP BY 1, 2, 3 ORDER BY 4 DESC
        """,
    )

    matching = q(
        MatchingCell,
        """
        WITH ce AS (
            SELECT DISTINCT ON (message_id) message_id, kind
            FROM email_events ORDER BY message_id, id DESC
        ),
        cm AS (
            SELECT DISTINCT ON (message_id) message_id, application_id, method
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT ce.kind,
               coalesce(cm.method, 'never attempted') AS method,
               count(*) AS messages
        FROM email_messages m
        JOIN ce ON ce.message_id = m.id
        LEFT JOIN cm ON cm.message_id = m.id
        WHERE ce.kind <> 'not_job_related' {window}
        GROUP BY 1, 2 ORDER BY 3 DESC
        """,
    )

    # The prefilter gates nothing on purpose - a filtered-out email is the one
    # unrecoverable failure, because the posting is closed and the thread is
    # not coming back. It survives to measure, after the fact, how much a gate
    # WOULD have missed. That is the only honest basis for ever letting the
    # ongoing feed use one, and it is a question only this endpoint can answer.
    prefilter = q(
        PrefilterCell,
        """
        WITH ce AS (
            SELECT DISTINCT ON (message_id) message_id, kind
            FROM email_events ORDER BY message_id, id DESC
        )
        SELECT coalesce(m.prefilter_hit, false) AS prefilter_hit,
               ce.kind <> 'not_job_related' AS job_related,
               count(*) AS messages
        FROM email_messages m JOIN ce ON ce.message_id = m.id
        WHERE TRUE {window}
        GROUP BY 1, 2
        """,
    )
    missed = sum(r.messages for r in prefilter if not r.prefilter_hit and r.job_related)
    job_related_total = sum(r.messages for r in prefilter if r.job_related)

    senders = q(
        SenderDomainRow,
        """
        WITH ce AS (
            SELECT DISTINCT ON (message_id) message_id, kind
            FROM email_events ORDER BY message_id, id DESC
        ),
        cm AS (
            SELECT DISTINCT ON (message_id) message_id, application_id
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT split_part(lower(m.from_email), '@', 2) AS domain,
               count(*) AS messages,
               count(*) FILTER (WHERE cm.application_id IS NOT NULL) AS matched
        FROM email_messages m
        JOIN ce ON ce.message_id = m.id
        LEFT JOIN cm ON cm.message_id = m.id
        WHERE ce.kind <> 'not_job_related' {window}
        GROUP BY 1 ORDER BY 2 DESC LIMIT 40
        """,
    )

    # Deliberately NOT windowed, and named `corpus` so it cannot be read as
    # though it were. "How much mail exists, when does it start, how much is
    # still unclassified" are properties of the import, not of a slice: the
    # backlog question is about the whole eight-year backfill. by_source is
    # unwindowed for the same reason - it used to interpolate {window} while
    # the totals above it did not, so at days=30 the breakdown summed to less
    # than the total it was nested under and `oldest` reported 2018 on a
    # screen labelled "last 30 days".
    corpus = (
        db.query_one(
            """
        SELECT count(*) AS messages,
               count(*) FILTER (
                   WHERE NOT EXISTS (SELECT 1 FROM email_events e WHERE e.message_id = m.id)
               ) AS unclassified,
               min(m.sent_at) AS oldest,
               max(m.sent_at) AS newest
        FROM email_messages m
        """
        )
        or {}
    )
    domain_total = db.query_one(
        "SELECT count(DISTINCT split_part(lower(m.from_email), '@', 2)) AS domains "
        "FROM email_messages m JOIN ("
        "  SELECT DISTINCT ON (message_id) message_id, kind FROM email_events"
        "  ORDER BY message_id, id DESC) ce ON ce.message_id = m.id "
        f"WHERE ce.kind <> 'not_job_related' {window}",
        params,
    )

    return MailAnalytics(
        window_days=days or None,
        corpus=MailCorpus(
            messages=corpus.get("messages", 0),
            unclassified=corpus.get("unclassified", 0),
            oldest=corpus.get("oldest"),
            newest=corpus.get("newest"),
            by_source=db.query_as(
                SourceRow,
                "SELECT m.source, count(*) AS messages FROM email_messages m "
                "GROUP BY 1 ORDER BY 2 DESC",
            ),
        ),
        # Each section below counts a DIFFERENT population, and nothing in the
        # rows says so. Presented side by side they invite a subtraction that
        # means nothing, so the denominators ship here rather than being
        # hardcoded by whoever renders them.
        populations=Populations(
            classification=Population(
                messages=sum(r.messages for r in classification), excludes=[]
            ),
            matching=Population(
                messages=sum(r.messages for r in matching), excludes=["not_job_related"]
            ),
            prefilter=Population(messages=sum(r.messages for r in prefilter), excludes=[]),
            sender_domains=DomainPopulation(
                messages=sum(r.messages for r in senders),
                excludes=["not_job_related"],
                domains_shown=len(senders),
                domains_total=(domain_total or {}).get("domains", 0),
            ),
        ),
        classification=classification,
        matching=matching,
        prefilter=PrefilterSummary(
            cells=prefilter,
            job_related_a_gate_would_have_dropped=missed,
            job_related_total=job_related_total,
        ),
        sender_domains=senders,
    )


class AdminMailMessage(BaseModel):
    """The stored message, whole, for the one surface that may see everybody's.

    The columns were a `SELECT *`, so what reached the client was whatever the
    table happened to hold on the day. `body_html` is the markup as it
    arrived: the user-facing reader sanitises on read, this one does not,
    because an administrator debugging a classification needs what the
    classifier saw."""

    id: int
    user_id: int
    provider_message_id: str
    provider_thread_id: str | None
    thread_topic: str | None
    source: str
    from_email: str | None
    from_name: str | None
    to_emails: list[str] | None
    subject: str | None
    sent_at: datetime.datetime | None
    body_text: str | None
    body_html: str | None
    headers: dict[str, Any] | None
    prefilter_hit: bool | None
    prefilter_reason: str | None
    imported_at: datetime.datetime


class AdminMailEvent(BaseModel):
    """One classification, as written. The log is append-only, so a message
    with three of these was corrected twice and the last one is in force."""

    id: int
    message_id: int
    kind: str
    confidence: str | None
    occurred_at: datetime.datetime | None
    deadline_at: datetime.datetime | None
    deadline_inferred: bool
    detail: dict[str, Any] | None
    # Which machine wrote it; null means a person did, and actor_user_id says
    # which one.
    model: str | None
    actor_user_id: int | None
    created_at: datetime.datetime


class AdminMailMatch(BaseModel):
    """One match attempt, with the application it named. Append-only as well,
    so a tier that keeps being corrected stays visible as history rather than
    being papered over one row at a time."""

    id: int
    message_id: int
    application_id: int | None
    method: str
    confidence: str | None
    rationale: str | None
    actor_user_id: int | None
    created_at: datetime.datetime
    company_name: str | None
    title: str | None
    job_id: int | None


class AdminMailDetail(BaseModel):
    message: AdminMailMessage
    events: list[AdminMailEvent]
    matches: list[AdminMailMatch]
    # What tier 1 would see, so a missed exact-link match can be diagnosed
    # without re-running the matcher and guessing at why.
    canonical_urls: list[str]


@router.get("/admin/mail/{message_id}")
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
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown message"})
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


class MatchOverride(BaseModel):
    application_id: int | None
    # WHICH no-match this is, when application_id is null. Without it every
    # admin no-match was recorded as method='manual' with a null application,
    # which the unmatched predicate reads as a matcher FAILURE - so an admin
    # who correctly decided a recruiter approach belongs to no application put
    # it straight back into the queue of things needing attention, and nothing
    # said so. `not_an_application` is not `unmatched`: deliberately attached
    # to nothing versus looked and found nothing, a distinction that has now
    # mattered on six surfaces.
    #
    # Default keeps the old behaviour for callers that do not send it: a
    # no-match with no reason given is a failure to find one, which is the
    # weaker claim and the safe one.
    outcome: Literal["no_application_found", "not_an_application"] = "no_application_found"


def _admin_message(message_id: int) -> dict[str, Any]:
    """Any user's message, for an administrator.

    Deliberately NOT `_owned_message`: the whole point of these routes is that
    the admin corrects other people's mailboxes - friends and family, not a
    hypothetical. The ownership rule that applies is `require_admin` on the
    route; what this returns is the OWNER, because every helper below needs to
    be scoped to them rather than to the caller.
    """
    message = db.query_one(
        "SELECT id, user_id, subject, from_email, sent_at, body_text, provider_thread_id "
        "FROM email_messages WHERE id = %s",
        (message_id,),
    )
    if message is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown message"})
    return message


@router.get("/admin/mail/{message_id}/candidates")
def admin_match_candidates(
    message_id: int,
    q: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    user: AuthedUser = Depends(require_admin),
) -> Candidates:
    """The same picker the user gets, over the message owner's applications.

    The admin panel offered a bare application-id field, which requires
    knowing an id that is not displayed anywhere - so the only correction
    available in practice was no correction. This is the identical ranking the
    user side computes, including the candidates `_by_company` refused to
    choose between, which are the ones a person is best placed to settle.
    """
    message = _admin_message(message_id)
    return _candidates_payload(message, message["user_id"], q, limit)


@router.post("/admin/mail/{message_id}/classify")
def admin_correct_classification(
    message_id: int, body: Reclassification, user: AuthedUser = Depends(require_admin)
) -> Reclassified:
    """Correct what a message IS, in someone else's mailbox.

    Recorded against the admin rather than the owner: `actor_user_id` is the
    caller, so a later reader can tell an administrator's correction from the
    owner's own without a second flag saying which.
    """
    message = _admin_message(message_id)
    return _apply_classification(message, body, actor_user_id=user.id)


@router.post("/admin/mail/{message_id}/classify/revert", response_model_exclude_none=True)
def admin_revert_classification(
    message_id: int, user: AuthedUser = Depends(require_admin)
) -> Reverted:
    """Undo a correction by restoring the model's last answer."""
    _admin_message(message_id)
    return _apply_revert(message_id, actor_user_id=user.id)


class MatchOverridden(BaseModel):
    """The match now in force, which is what the append produced rather than
    what was asked for - a refusal is recorded as the matcher's own refusal
    method, so agreeing with the matcher looks like agreeing with it."""

    ok: bool
    current: CurrentMatch | None


@router.post("/admin/mail/{message_id}/match")
def override_match(
    message_id: int, body: MatchOverride, user: AuthedUser = Depends(require_admin)
) -> MatchOverridden:
    """Correct a match by hand.

    An append, not an edit: the matcher's own attempt survives underneath, so
    a systematically wrong tier stays visible in the history instead of being
    quietly papered over one row at a time. That history is the only evidence
    that the matcher needs fixing rather than the row.

    A null application_id needs `outcome` to say WHICH no-match is meant,
    because the two are not the same fact and the queue treats them
    differently. Attaching to an application ignores it - the outcome of a
    match is the match.
    """
    message = db.query_one("SELECT user_id FROM email_messages WHERE id = %s", (message_id,))
    if not message:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown message"})
    if body.application_id is not None:
        owner = db.query_one(
            "SELECT user_id FROM applications WHERE id = %s", (body.application_id,)
        )
        if not owner or owner["user_id"] != message["user_id"]:
            # Cross-user match would attribute one person's outcome to
            # another's application. 404 rather than 403: whether that
            # application exists is not something the caller is entitled to.
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown application"})
    # A deliberate refusal is recorded as the matcher's own refusal method, so
    # every reader that already distinguishes the two - the unmatched cut, the
    # analytics breakdown, the pipeline filter - sees it without being taught a
    # fourth value. Agreeing with the matcher should look like agreeing with it.
    if body.application_id is None and body.outcome == "not_an_application":
        method, confidence = mail_match.NOT_AN_APPLICATION, "high"
    else:
        method = mail_match.MANUAL
        confidence = "high" if body.application_id is not None else "none"
    mail_match.record(
        message_id,
        actor_user_id=user.id,
        match=mail_match.Match(body.application_id, method, confidence, f"set by admin {user.sub}"),
    )
    if body.application_id is not None:
        mail_pipeline.sync_action_items(body.application_id)
    return MatchOverridden(ok=True, current=mail_match.latest(message_id))
