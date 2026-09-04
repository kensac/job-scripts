"""Board membership: which jobs are on a user's board, and why.

The visibility predicate is spelled here and in routers/jobs.py; the two must
change together or the read path and the write path drift.
"""

from __future__ import annotations

import logging
from typing import Any

from api import db, metrics

logger = logging.getLogger("jobtracker_worker")


# A board row counts as untouched (machine-managed) when the user never set
# anything on it; only these are auto-added by materialization and auto-removed
# by re-verification.
_UNTOUCHED = """
    (uj.status IS NULL OR uj.status = '') AND uj.date_applied IS NULL
    AND COALESCE(uj.notes, '') = '' AND COALESCE(uj.size, '') = ''
    AND COALESCE(uj.recruiter, '') = '' AND COALESCE(uj.connection1, '') = ''
    AND COALESCE(uj.connection2, '') = '' AND COALESCE(uj.documents, '') = ''
    AND NOT uj.hidden
"""


def _materialize_passing(user_id: int) -> int:
    """Mirror of the old write_to_sheet step: every job currently passing ALL
    of the user's enabled filters (and the structural gates) becomes a board
    row. Existing rows (including hidden ones) are untouched, so deleting a
    row means 'bring it back next run if it still passes' while hiding is
    permanent."""
    from api import criteria as crit

    settings = db.query_one(
        "SELECT bypass_sponsorship_filter, criteria FROM user_settings WHERE user_id = %s",
        (user_id,),
    )
    params = {
        "uid": user_id,
        "bypass": settings["bypass_sponsorship_filter"] if settings else True,
        **crit.params(settings),
    }
    with db.pool.connection() as conn:
        result = conn.execute(
            f"""
            WITH enabled AS (
                -- DISTINCT for the same reason as _VISIBILITY: duplicate
                -- prompt_hashes cancel out in this query's symmetric counts,
                -- but the two predicates must stay spelled the same way or
                -- the read path and the write path drift apart again.
                SELECT DISTINCT prompt_hash FROM user_filters
                WHERE user_id = %(uid)s AND enabled
            ),
            latest_check AS (
                SELECT DISTINCT ON (url, check_type) url, check_type, status
                FROM ai_queries
                WHERE check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')
                ORDER BY url, check_type, id DESC
            ),
            pass_all AS (
                SELECT j.id FROM jobs j
                WHERE (j.source IN (SELECT source FROM user_sources WHERE user_id = %(uid)s)
                       OR j.source = 'sheet_import' OR j.uploaded_by = %(uid)s)
                  AND j.active
                  {crit.SQL}
                  AND EXISTS (SELECT 1 FROM latest_check lc WHERE lc.url = j.url
                              AND lc.check_type = 'closed' AND lc.status = 'passed')
                  AND (%(bypass)s OR EXISTS (SELECT 1 FROM latest_check lc WHERE lc.url = j.url
                              AND lc.check_type = 'clearance' AND lc.status = 'passed'))
                  AND (SELECT COUNT(*) FROM enabled) > 0
                  AND (SELECT COUNT(*) FROM enabled e WHERE (
                        SELECT status FROM ai_queries q WHERE q.url = j.url
                          AND q.check_type = 'custom' AND q.prompt_hash = e.prompt_hash
                          AND q.status IN ('passed', 'rejected')
                        ORDER BY q.id DESC LIMIT 1) = 'passed') = (SELECT COUNT(*) FROM enabled)
            )
            INSERT INTO user_jobs (user_id, job_id)
            SELECT %(uid)s, id FROM pass_all
            ON CONFLICT DO NOTHING
            """,
            params,
        )
        added = result.rowcount
    if added:
        metrics.BOARD_ROWS.labels("materialized").inc(added)
        logger.info(f"Materialized {added} passing jobs onto user {user_id}'s board")
    return added


def _candidates(user_id: int) -> list[dict[str, Any]]:
    from api import criteria

    settings = db.query_one(
        "SELECT bypass_sponsorship_filter, criteria FROM user_settings WHERE user_id = %s",
        (user_id,),
    )
    return db.query(
        f"""
        WITH latest_check AS (
            SELECT DISTINCT ON (url, check_type) url, check_type, status
            FROM ai_queries
            WHERE check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')
            ORDER BY url, check_type, id DESC
        )
        SELECT j.url, j.company, j.title FROM jobs j
        WHERE j.active
          AND (j.source IN (SELECT source FROM user_sources WHERE user_id = %(uid)s)
               OR j.uploaded_by = %(uid)s)
          {criteria.SQL}
          AND EXISTS (SELECT 1 FROM latest_check lc
                      WHERE lc.url = j.url AND lc.check_type = 'closed' AND lc.status = 'passed')
          AND (%(bypass)s
               OR EXISTS (SELECT 1 FROM latest_check lc
                          WHERE lc.url = j.url AND lc.check_type = 'clearance' AND lc.status = 'passed'))
        ORDER BY j.id DESC
        """,
        {
            "uid": user_id,
            "bypass": settings["bypass_sponsorship_filter"] if settings else True,
            **criteria.params(settings),
        },
    )


def _decided_urls(urls: list[str], prompt_hash: str, model: str) -> set:
    """URLs that already have a decided verdict for this filter+model - one
    query instead of one per job, so cache-hit reruns cost nothing per row."""
    if not urls:
        return set()
    rows = db.query(
        "SELECT DISTINCT url FROM ai_queries WHERE url = ANY(%s) "
        "AND check_type = 'custom' AND prompt_hash = %s AND model = %s "
        "AND status IN ('passed', 'rejected')",
        (urls, prompt_hash, model),
    )
    return {r["url"] for r in rows}


def _content_ready_urls(urls: list[str]) -> set:
    if not urls:
        return set()
    rows = db.query(
        "SELECT DISTINCT url FROM ai_queries WHERE url = ANY(%s) "
        "AND check_type != 'custom' AND input_content IS NOT NULL AND input_content != ''",
        (urls,),
    )
    return {r["url"] for r in rows}


def fetch_retry_interval() -> str:
    """How long a posting whose fetch came back empty waits before any ingest
    or backfill tries it again, as a Postgres interval. Read from the persisted
    admin config on every call, so a change on the config page takes effect
    on the next cycle; the seeded default is 24 hours (api/db.py)."""
    return f"{int(db.get_config('fetch_retry_after_hours'))} hours"


def _content_attempted_urls(urls: list[str]) -> set:
    """URLs whose page was fetched, with or without a result, inside the retry
    window. A failed fetch leaves a 'failed' content row (verdicts.refresh_content)
    and that row is the only memory of the attempt."""
    if not urls:
        return set()
    rows = db.query(
        "SELECT DISTINCT url FROM ai_queries WHERE url = ANY(%s) AND check_type = 'content' "
        "AND created_at > now() - %s::interval",
        (urls, fetch_retry_interval()),
    )
    return {r["url"] for r in rows}


def _demote_closed() -> int:
    with db.pool.connection() as conn:
        result = conn.execute(
            f"""
            DELETE FROM user_jobs uj USING jobs j
            WHERE uj.job_id = j.id AND {_UNTOUCHED}
              AND (
                -- A posting that vanished from its source feed is gone even
                -- if no closed-check ever ran on it. Keying only on the
                -- verdict left dead postings sitting in the intake view
                -- forever, because ingest marks them inactive and the sweep
                -- never looks at them again.
                NOT j.active
                OR (SELECT q.status FROM ai_queries q WHERE q.url = j.url
                    AND q.check_type = 'closed' AND q.status IN ('passed', 'rejected')
                    ORDER BY q.id DESC LIMIT 1) = 'rejected'
              )
            """
        )
        demoted = result.rowcount
    if demoted:
        metrics.BOARD_ROWS.labels("demoted").inc(demoted)
        logger.info(f"Demoted {demoted} closed rows from boards")
    return demoted
