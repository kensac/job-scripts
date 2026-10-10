"""Board membership: which jobs are on a user's board, and why.

Every name here is public, because every one of them was already imported by
another module while spelled private. candidates_for is not called `candidates`
because tasks/filters.py binds a local of that name around the call, where the
short name would shadow the function rather than read as it.

The predicate itself is not spelled here. api/visibility.py owns it: FULL is
the one spelling, and handle_recompute_board below runs it through
visibility.recompute. What this module does spell is the write path around it,
materialize_passing and candidates_for, which share criteria.SQL and the
structural gates with FULL and must stay consistent with it.
"""

from __future__ import annotations

import logging
from typing import Any

from api import db, metrics
from api.board import criteria
from api.board import eligibility as board_eligibility
from core import verdict_reads

logger = logging.getLogger(__name__)


# The working set is what the filters picked for a person: user_job_working_set,
# written here and pruned by demote_closed. It carries scope (core/store.py's
# ON_A_BOARD makes the posting worth paying to check, and handle_reverify_open
# draws its candidates from it). It does not decide what the person sees:
# visibility.FULL admits a picked posting through its structural branch.
#
# user_jobs holds only what a person did (phase 2b). Machine rows were written
# there too until 2026-10-10; they were working-set membership, copied into
# the working set and then removed from user_jobs.
def materialize_passing(user_id: int) -> int:
    """Every job currently passing ALL of the user's enabled filters (and the
    structural gates) joins the person's working set. Returns how many joined.

    This does NOT decide what the person sees and writes nothing a person
    owns. It used to insert an empty user_jobs row, a mirror of the step that
    wrote a Google Sheet, where the row WAS the board."""
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
                       OR j.source = 'sheet_import' OR j.uploaded_by = %(uid)s)
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
          AND ({board_eligibility.SUBSCRIBED} OR j.uploaded_by = %(uid)s)
        ORDER BY j.id DESC
        """,
        board_eligibility.settings_params(user_id),
    )


# The URLs a filter chunk holds, in either payload shape (api.task_jobs): the
# legacy inline `jobs` list, or `urls`, kept inline beside the referenced list
# because these readers run in SQL and cannot follow a reference. A payload
# holds one or the other, so this is exactly the URL sequence of its jobs.
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
    """URLs a live filter chunk of an earlier run still holds for this user.

    A run that splits while those chunks wait at the provider must not
    submit them again. A batch yields nothing until it is terminal, so a
    chunk parked on a straggler holds every url in it undecided for hours,
    and the verdict cache cannot see them; re-selecting would pay twice for the
    same verdicts. Excluding them is what lets a new run start while an old
    one is still parked: it judges only what arrived since.
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
                -- A posting that vanished from its source feed is gone even
                -- if no closed-check ever ran on it. Keying only on the
                -- verdict left dead postings sitting in the intake view
                -- forever, because ingest marks them inactive and the sweep
                -- never looks at them again.
                NOT j.active
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
