"""Which new postings the verify sweep asks about.

The sweep's candidate query (gated by api.verification_candidates), the
backfill query for postings named by url, and the postings other parked
sweeps already submitted.
"""

from __future__ import annotations

from typing import Any

from api import ai_pause, db
from core import catalog, verdict_reads
from core.store import CONTENT_LATERAL, MIN_CONTENT_CHARS


def _in_flight(task_id: int) -> list[str]:
    """Postings another parked sweep has already submitted.

    A sweep no longer waits for the previous one to finish (a straggling batch
    held one for 15 hours on 2026-10-08, and no new posting was verified until
    it returned), so each excludes what the others are already paying for.
    """
    return [
        row["custom_id"]
        for row in db.query(
            "SELECT r.custom_id FROM batch_requests r JOIN tasks t ON t.id = r.task_id "
            "WHERE t.kind = 'verify_new' AND t.id <> %s "
            "AND t.status IN ('pending', 'running', 'waiting', 'awaiting_batch')",
            (task_id,),
        )
    ]


def candidates_sql() -> str:
    """The verify sweep's candidates, freshest first. Binds
    verification_candidates.params() plus `cap` and `in_flight`.

    The filters run in the order that keeps each one's input small.
    Availability and a missing verdict are one scan of jobs against two
    hashed sets. The gate (verification_candidates.reachable) runs on what
    that leaves, the per-posting probe for page text on what the gate admits,
    and only the capped rows read the text itself. Run as one predicate over
    every posting, with the text read before the sort, the query ran past 14
    minutes on production (2026-10-10) and spilled to disk.
    """
    from api import verification_candidates

    return f"""
    WITH {verification_candidates.TARGETS},
    pool AS MATERIALIZED (
        SELECT j.id, ({verification_candidates.EVERY_TARGET}) AS every_target
        FROM jobs j
        WHERE {catalog.IS_AVAILABLE.format(job="j")}
          AND NOT (j.url = ANY(%(in_flight)s::text[]))
          -- A pause's backlog is not bought when AI comes back (api.ai_pause).
          AND NOT {ai_pause.SKIPPED.format(job="j")} AND (
            NOT {verdict_reads.has_verdict("j.url", "closed")}
            -- Short-circuited pipelines (and any upstream verdict that later
            -- flips to passing) leave downstream checks MISSING, not false;
            -- a job invisible for want of a clearance verdict never heals
            -- unless the sweep looks for holes in every check, not just the
            -- first one.
            OR NOT {verdict_reads.has_verdict("j.url", "clearance")}
        )
    ),
    reached AS MATERIALIZED (
        {verification_candidates.reachable("pool")}
    ),
    chosen AS (
        SELECT r.id, r.date_posted FROM reached r
        -- A scalar subquery, which the planner runs per reached posting. As
        -- a join and an EXISTS it drove the check from all 840,000 fetched
        -- urls instead (60 s on production, 2026-10-10).
        WHERE (
            SELECT EXISTS (
                SELECT 1 FROM page_texts q
                WHERE q.url = j.url AND q.chars > {MIN_CONTENT_CHARS})
            FROM jobs j WHERE j.id = r.id
        )
        -- Freshest first, as the content sweep already selects. The cap
        -- makes this a priority queue, and `j.id` is ingest order, so a
        -- backlog starves the day's postings: on 2026-09-15, 214,306
        -- active postings held no closed verdict and only 3,440 of them
        -- were posted within three days. A posting that arrived that
        -- morning waited out fifty cycles behind postings months old.
        -- Board candidacy requires this verdict, so the managed boards
        -- showed nothing new while the sweep worked. Ordered, one cycle
        -- covers every genuinely fresh posting and the stale remainder
        -- drains behind it.
        ORDER BY r.date_posted DESC NULLS LAST
        LIMIT %(cap)s
    )
    SELECT j.url, j.source, j.company, j.title, q.input_content, q.content_ref,
           q.id AS page_fetch_id,
           NOT {verdict_reads.has_verdict("j.url", "closed")} AS needs_closed,
           NOT {verdict_reads.has_verdict("j.url", "clearance")} AS needs_clearance
    FROM chosen c JOIN jobs j ON j.id = c.id
    {CONTENT_LATERAL.format(url="j.url", columns="id, input_content, content_ref")}
    ORDER BY c.date_posted DESC NULLS LAST
    """


def listed_sql() -> str:
    """The named postings still missing a closed or clearance verdict. Binds
    `urls` and `in_flight`.

    For a backfill an administrator asks for by url: the postings the sweep's
    gate (verification_candidates.reachable) and cap held back still have no
    verdict, so nothing records what happened to them. The availability,
    missing-verdict, in-flight and page-text conditions are the sweep's own."""
    return f"""
    SELECT j.url, j.source, j.company, j.title, q.input_content, q.content_ref,
           q.id AS page_fetch_id,
           NOT {verdict_reads.has_verdict("j.url", "closed")} AS needs_closed,
           NOT {verdict_reads.has_verdict("j.url", "clearance")} AS needs_clearance
    FROM jobs j
    {CONTENT_LATERAL.format(url="j.url", columns="id, input_content, content_ref")}
    WHERE j.url = ANY(%(urls)s::text[])
      AND {catalog.IS_AVAILABLE.format(job="j")}
      AND NOT (j.url = ANY(%(in_flight)s::text[]))
      AND (
        NOT {verdict_reads.has_verdict("j.url", "closed")}
        OR NOT {verdict_reads.has_verdict("j.url", "clearance")}
      )
    ORDER BY j.date_posted DESC NULLS LAST
    """


def _select_postings(task_id: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The postings this sweep asks about: the named ones for a requested
    backfill, past the gate and cap, or the capped candidates otherwise."""
    from api import verification_candidates

    if "urls" in payload:
        return db.query(listed_sql(), {"urls": payload["urls"], "in_flight": _in_flight(task_id)})
    candidate_params = {
        **verification_candidates.params(),
        "cap": int(db.get_config("verify_new_per_cycle")),
        "in_flight": _in_flight(task_id),
    }
    # Without JIT. Its cost estimate is in the millions, so JIT compiles
    # the whole plan, and on production (2026-10-10) the same plan took
    # 78 s with it and 24 s without: 38 s went to hashing the
    # 16,000 targets.
    with db.transaction():
        db.execute("SET LOCAL jit = off")
        return db.query(candidates_sql(), candidate_params)
