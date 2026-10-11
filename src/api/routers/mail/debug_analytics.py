"""Read mail consistency and pipeline distributions without changing classification."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api import db, rates
from api.auth import AuthedUser
from api.mail.current import current_event, current_match
from api.routers.admin import require_admin
from api.routers.mail.debug_models import (
    ClassificationCell,
    ClassificationConsistency,
    ConsistencyCoverage,
    ConsistencyGroup,
    DomainPopulation,
    MailAnalytics,
    MailCorpus,
    MatchingCell,
    Population,
    Populations,
    PrefilterCell,
    PrefilterSummary,
    SenderDomainRow,
    SourceRow,
)

router = APIRouter()

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
_CONSISTENCY_SQL = f"""
WITH latest AS (
    {current_event("kind")}
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
        f"""
        WITH latest AS (
            {current_event()}
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

    def query_window_rows[Row](row: type[Row], sql: str) -> list[Row]:
        return db.query_as(row, sql, params)

    classification = query_window_rows(
        ClassificationCell,
        f"""
        WITH ce AS (
            {current_event("kind", "confidence", "model", "deadline_inferred", "detail")}
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

    matching = query_window_rows(
        MatchingCell,
        f"""
        WITH ce AS (
            {current_event("kind")}
        ),
        cm AS (
            {current_match("application_id", "method")}
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
    prefilter = query_window_rows(
        PrefilterCell,
        f"""
        WITH ce AS (
            {current_event("kind")}
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

    senders = query_window_rows(
        SenderDomainRow,
        f"""
        WITH ce AS (
            {current_event("kind")}
        ),
        cm AS (
            {current_match("application_id")}
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
        f"FROM email_messages m JOIN ({current_event('kind')}) ce ON ce.message_id = m.id "
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
