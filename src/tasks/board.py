"""Maintain the jobs selected by a person's filters.

``api.board.visibility`` owns what the person can see. This module writes the
working set and selects candidates using the same structural rules. Public
function names stay distinct from local names in their callers.
"""

from __future__ import annotations

import logging
from typing import Any

from api import db, metrics
from api.board import criteria
from api.board import eligibility as board_eligibility
from core import catalog, verdict_reads

logger = logging.getLogger(__name__)


# The working set records filter picks. It keeps those jobs in scope for
# verification; visibility decides whether the person can see them.
# user_jobs stores actions a person took, not filter picks.
def materialize_passing(user_id: int) -> int:
    """Add jobs passing all enabled filters and structural gates to the working set.

    Return the number added. Visibility and person-owned rows are unchanged.
    """
    params = board_eligibility.settings_params(user_id)
    filter_status = verdict_reads.latest_status("j.url", "custom", prompt_hash="e.prompt_hash")
    result = db.query_one(
        f"""
            WITH enabled AS (
                {board_eligibility.ENABLED_FILTERS}
            ),
            {board_eligibility.LATEST_CHECK},
            pass_all AS MATERIALIZED (
                SELECT j.id FROM jobs j
                WHERE ({board_eligibility.SUBSCRIBED}
                       OR j.source = 'sheet_import'
                       OR j.id IN (SELECT job_id FROM posting_uploads WHERE uploaded_by = %(uid)s))
                  AND {board_eligibility.STRUCTURAL.format(criteria=criteria.SQL)}
                  AND (SELECT COUNT(*) FROM enabled) > 0
                  AND (SELECT COUNT(*) FROM enabled e WHERE {filter_status} = 'passed')
                      = (SELECT COUNT(*) FROM enabled)
            ),
            working_set_insert AS (
                INSERT INTO user_job_working_set (user_id, job_id)
                SELECT %(uid)s, id FROM pass_all ORDER BY id
                ON CONFLICT (user_id, job_id) DO NOTHING
                RETURNING 1
            )
            SELECT COUNT(*) AS added FROM working_set_insert
        """,
        params,
    )
    assert result is not None
    added = result["added"]
    if added:
        metrics.BOARD_ROWS.labels("materialized").inc(added)
        logger.info(f"Materialized {added} passing jobs onto user {user_id}'s board")
    return added


def candidates_for(user_id: int) -> list[dict[str, Any]]:
    return db.query(
        f"""
        WITH {board_eligibility.LATEST_CHECK}
        SELECT j.url, j.company, j.title, j.source FROM jobs j
        WHERE {board_eligibility.STRUCTURAL.format(criteria=criteria.SQL)}
          AND ({board_eligibility.SUBSCRIBED}
               OR j.id IN (SELECT job_id FROM posting_uploads WHERE uploaded_by = %(uid)s))
        ORDER BY j.id DESC
        """,
        board_eligibility.settings_params(user_id),
    )


# Filter chunks have two active payload shapes: inline jobs, or a referenced
# list with inline URLs. SQL readers use whichever URLs the payload holds.
CHUNK_URLS = (
    "CROSS JOIN LATERAL (SELECT j->>'url' AS url FROM jsonb_array_elements({t}.payload->'jobs') j "
    "UNION ALL SELECT jsonb_array_elements_text({t}.payload->'urls')) AS chunk_url"
)


def submission_exclusions(
    task_id: int, user_id: int, urls: list[str], prompt_hash: str, model: str
) -> set[str]:
    """Recheck decisions and deterministic chunk ownership in one database snapshot.

    Planning can precede submission by hours. Checking decisions and owners in
    separate statements also races a collector committing its verdict and then
    completing its task. The older chunk owns overlapping work, regardless of
    which worker reaches submission first. No lock spans provider network I/O.
    """
    if not urls:
        return set()
    rows = db.query(
        f"""
        WITH owners AS MATERIALIZED (
            SELECT t.payload FROM tasks t
            WHERE t.id < %(tid)s
              AND t.kind IN ('run_filter_chunk', 'run_filter_batch_chunk')
              AND (t.payload->>'user_id')::bigint = %(uid)s
              -- A chunk names its filter by config_id (api.run_configs).
              AND (t.payload->>'config_id')::bigint IN (
                SELECT id FROM run_configs
                WHERE kind = 'filter' AND body->>'prompt_hash' = %(hash)s)
              AND (t.status IN ('pending', 'running', 'waiting', 'awaiting_batch')
                   OR jsonb_array_length(COALESCE(t.payload->'batch_ids', '[]'::jsonb)) > 0)
        )
        SELECT DISTINCT url FROM verdicts
        WHERE url = ANY(%(urls)s) AND check_type = 'custom'
          AND prompt_hash = %(hash)s AND model = %(model)s
        UNION
        SELECT chunk_url.url FROM owners {CHUNK_URLS.format(t="owners")}
        WHERE chunk_url.url = ANY(%(urls)s)
        """,
        {"tid": task_id, "uid": user_id, "urls": urls, "hash": prompt_hash, "model": model},
    )
    return {row["url"] for row in rows}


def in_flight_urls(user_id: int) -> set:
    """URLs held by this person's live filter chunks.

    Exclude them from new runs until those chunks finish. A parked provider
    batch has no verdicts yet, so the verdict cache cannot prevent duplicate
    submissions on its own.
    """
    rows = db.query(
        f"SELECT DISTINCT chunk_url.url FROM tasks t {CHUNK_URLS.format(t='t')} "
        "WHERE t.kind IN ('run_filter_chunk', 'run_filter_batch_chunk') "
        "AND t.status IN ('pending', 'running', 'awaiting_batch') "
        "AND (t.payload->>'user_id')::bigint = %s",
        (user_id,),
    )
    return {r["url"] for r in rows}


def content_ready_urls(urls: list[str]) -> set:
    if not urls:
        return set()
    rows = db.query(
        "SELECT DISTINCT url FROM page_texts WHERE url = ANY(%s)",
        (urls,),
    )
    return {r["url"] for r in rows}


def fetch_retry_interval() -> str:
    """How long a posting whose fetch came back empty waits before any ingest
    or backfill tries it again, as a Postgres interval. Read from the persisted
    admin config on every call, so a change on the config page takes effect
    on the next cycle; the seeded default is 24 hours (api/db.py)."""
    return f"{int(db.get_config('fetch_retry_after_hours'))} hours"


def demote_closed() -> int:
    result = db.query_one(
        f"""
            WITH demotable AS MATERIALIZED (
                SELECT membership.user_id, membership.job_id
                FROM user_job_working_set membership
                JOIN jobs j ON j.id = membership.job_id
                WHERE (
                -- A posting no longer available (catalog.IS_AVAILABLE) is gone
                -- even if no closed-check ever ran on it. Keying only on the
                -- verdict left dead postings sitting in the intake view
                -- forever, because ingest marks them inactive and the sweep
                -- never looks at them again.
                NOT {catalog.IS_AVAILABLE.format(job="j")}
                OR {verdict_reads.latest_status("j.url", "closed")} = 'rejected'
                )
                ORDER BY membership.user_id, membership.job_id
            ),
            working_set_delete AS (
                DELETE FROM user_job_working_set working
                USING demotable
                WHERE working.user_id = demotable.user_id
                  AND working.job_id = demotable.job_id
                RETURNING 1
            )
            SELECT COUNT(*) AS demoted FROM working_set_delete
        """
    )
    assert result is not None
    demoted = result["demoted"]
    if demoted:
        metrics.BOARD_ROWS.labels("demoted").inc(demoted)
        logger.info(f"Demoted {demoted} closed rows from boards")
    return demoted


async def handle_recompute_board(task_id: int, payload: dict[str, Any]) -> None:
    """One person's board membership, from the full predicate, in place."""
    from api.board import visibility
    from tasks.runtime import set_progress

    user_id = int(payload["user_id"])
    n = visibility.recompute(user_id)
    set_progress(task_id, n, n, f"board recomputed: {n} postings")
    logger.info(f"board recomputed for user {user_id}: {n} postings")
