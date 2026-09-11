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

logger = logging.getLogger(__name__)


# A board row counts as untouched (machine-managed) when the user never set
# anything on it; only these are auto-added by materialization and auto-removed
# by re-verification.
#
# An untouched row is THE WORKING SET and not what a person sees. It carries
# scope: core/store.py's ON_A_BOARD makes the posting worth paying to check,
# and handle_reverify_open draws its candidates from these rows. Visibility is
# decided separately and does not consult them, because visibility.FULL admits
# an untouched row only through a branch that never references user_jobs.
#
# Touching a row moves it the other way: it becomes the person's, visible
# whatever a verdict says, and out of reach of both writers below.
UNTOUCHED = """
    (uj.status IS NULL OR uj.status = '') AND uj.date_applied IS NULL
    AND COALESCE(uj.notes, '') = '' AND COALESCE(uj.size, '') = ''
    AND COALESCE(uj.recruiter, '') = '' AND COALESCE(uj.connection1, '') = ''
    AND COALESCE(uj.connection2, '') = '' AND COALESCE(uj.documents, '') = ''
    AND NOT uj.hidden
"""


def materialize_passing(user_id: int) -> int:
    """Every job currently passing ALL of the user's enabled filters (and the
    structural gates) gets a board row. Existing rows (including hidden ones)
    are left alone, so deleting a row means "bring it back next run if it
    still passes" while hiding is permanent.

    This does NOT decide what the person sees. A passing posting is visible
    through visibility.FULL whether or not this has run, because FULL's
    structural branch does not reference user_jobs. What the row does is put
    the posting in the working set, which is what keeps it being checked.

    It used to be a mirror of the step that wrote a Google Sheet, where the
    row WAS the board. That sheet is gone and the row now means something
    else; the docstring said otherwise until 2026-09-10."""
    params = board_eligibility.settings_params(user_id)
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
                  AND (SELECT COUNT(*) FROM enabled e WHERE (
                        SELECT status FROM ai_queries q WHERE q.url = j.url
                          AND q.check_type = 'custom' AND q.prompt_hash = e.prompt_hash
                          AND q.status IN ('passed', 'rejected')
                        ORDER BY q.id DESC LIMIT 1) = 'passed') = (SELECT COUNT(*) FROM enabled)
            ),
            legacy_insert AS (
                INSERT INTO user_jobs (user_id, job_id)
                SELECT %(uid)s, id FROM pass_all ORDER BY id
                ON CONFLICT (user_id, job_id) DO NOTHING
                RETURNING 1
            ),
            working_set_insert AS (
                INSERT INTO user_job_working_set (user_id, job_id)
                SELECT %(uid)s, id FROM pass_all ORDER BY id
                ON CONFLICT (user_id, job_id) DO NOTHING
                RETURNING 1
            )
            SELECT COUNT(*) AS added FROM legacy_insert
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
        SELECT j.url, j.company, j.title FROM jobs j
        WHERE {board_eligibility.STRUCTURAL.format(criteria=criteria.SQL)}
          AND ({board_eligibility.SUBSCRIBED} OR j.uploaded_by = %(uid)s)
        ORDER BY j.id DESC
        """,
        board_eligibility.settings_params(user_id),
    )


def decided_urls(urls: list[str], prompt_hash: str, model: str) -> set:
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


def in_flight_urls(user_id: int) -> set:
    """URLs a live filter chunk of an earlier run still holds for this user.

    A run that splits while those chunks wait at the provider must not
    submit them again. A batch yields nothing until it is terminal, so a
    chunk parked on a straggler holds every url in it undecided for hours,
    and decided_urls cannot see them; re-selecting would pay twice for the
    same verdicts. Excluding them is what lets a new run start while an old
    one is still parked: it judges only what arrived since.
    """
    rows = db.query(
        "SELECT DISTINCT j->>'url' AS url FROM tasks t, "
        "jsonb_array_elements(t.payload->'jobs') AS j "
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


def content_attempted_urls(urls: list[str]) -> set:
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


def demote_closed() -> int:
    with db.pool.connection() as conn:
        result = conn.execute(
            f"""
            DELETE FROM user_jobs uj USING jobs j
            WHERE uj.job_id = j.id AND {UNTOUCHED}
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


async def handle_recompute_board(task_id: int, payload: dict[str, Any]) -> None:
    """One person's board membership, from the full predicate, in place."""
    from api.board import visibility
    from tasks.runtime import set_progress

    user_id = int(payload["user_id"])
    n = visibility.recompute(user_id)
    set_progress(task_id, n, n, f"board recomputed: {n} postings")
    logger.info(f"board recomputed for user {user_id}: {n} postings")
