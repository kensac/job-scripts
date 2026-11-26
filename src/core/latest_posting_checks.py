"""latest_posting_checks: fold the dirty urls into each posting's latest
answer to each posting check, and keep the projection equal to `verdicts`.

Triggers on ai_queries and jobs append the url of every answer or posting a
write touches to latest_posting_checks_dirty (migration d81c5e2b7a43). This
module is the projection's only writer:
- `fold` recomputes the jobs of every dirty url from `verdicts`, writes the
  rows that differ, and deletes the dirty rows it saw;
- `fold(everything=True)` does the same for every job: the first fill, and a
  daily check for a write the triggers could not see (replica mode, a
  TRUNCATE). Writing only the rows that differ, a run with nothing to fix
  locks nothing.

One fold at a time, waiting on an advisory lock, and each fold one statement, so
one snapshot: the dirty rows it deletes are exactly the ones whose answers
it read. A dirty row committed after the snapshot is not visible to the
delete and stays for the next fold; until then a reader reads that url from
`verdicts`, so the projection is never read behind.
"""

from __future__ import annotations

from typing import LiteralString, cast

from core import verdict_reads
from core.pool import pool

# The latest answer per (job, check) of every check `verdicts` holds but
# custom filters, which are keyed by prompt. By exclusion, as the triggers
# select, so a newly registered posting check needs no change here. Keyed
# on jobs.id through the join, as verdict_reads.latest_per describes.
_LATEST = cast(
    "LiteralString",
    verdict_reads.latest_per(
        "j.id, v.check_type",
        "j.id AS job_id, v.check_type, v.id AS verdict_id, v.status",
        "v.check_type <> 'custom' {jobs}",
        join="JOIN jobs j ON j.url = v.url",
    ),
)

_DIRTY_JOBS: LiteralString = """
    AND j.url IN (SELECT url FROM latest_posting_checks_dirty)
"""

# The jobs a fold recomputes, as a predicate on the projection's rows.
_SCOPE_DIRTY: LiteralString = """
    l.job_id IN (SELECT j.id FROM jobs j
                 WHERE j.url IN (SELECT url FROM latest_posting_checks_dirty))
"""

_FOLD: LiteralString = """
    WITH latest AS MATERIALIZED ({latest}),
    removed AS (
        DELETE FROM latest_posting_checks l
        WHERE {scope}
          AND NOT EXISTS (SELECT 1 FROM latest t
                          WHERE t.job_id = l.job_id AND t.check_type = l.check_type)
        RETURNING 1
    ),
    written AS (
        INSERT INTO latest_posting_checks (job_id, check_type, verdict_id, status)
        SELECT t.job_id, t.check_type, t.verdict_id, t.status FROM latest t
        WHERE NOT EXISTS (
            SELECT 1 FROM latest_posting_checks l
            WHERE l.job_id = t.job_id AND l.check_type = t.check_type
              AND l.verdict_id = t.verdict_id AND l.status = t.status)
        ORDER BY t.job_id, t.check_type
        ON CONFLICT (job_id, check_type) DO UPDATE
          SET verdict_id = EXCLUDED.verdict_id, status = EXCLUDED.status
        RETURNING 1
    ),
    cleared AS (
        DELETE FROM latest_posting_checks_dirty RETURNING 1
    )
    SELECT (SELECT count(*) FROM written) AS written,
           (SELECT count(*) FROM removed) AS removed,
           (SELECT count(*) FROM cleared) AS cleared
"""

# Every posting's latest answer to every posting check, exactly as `verdicts`
# gives it, as a statement of (job_id, check_type, status): for a read over
# the whole catalog, where a set is cheaper than a probe per posting. A job
# whose url has a dirty row is read live from `verdicts`; every other job's
# rows come from the projection, which the fold keeps current for it.
CURRENT: LiteralString = f"""
    SELECT l.job_id, l.check_type, l.status FROM latest_posting_checks l
    WHERE NOT EXISTS (
        SELECT 1 FROM latest_posting_checks_dirty d JOIN jobs jd ON jd.url = d.url
        WHERE jd.id = l.job_id)
    UNION ALL
    SELECT t.job_id, t.check_type, t.status FROM ({_LATEST.format(jobs=_DIRTY_JOBS)}) t
"""

# One fold at a time across the fleet.
_LOCK_KEY = "hashtextextended('latest_posting_checks', 0)"


def fold(*, everything: bool = False) -> dict[str, int]:
    """Bring the projection to `verdicts` for the dirty urls, or for every
    job. The rows written and removed and the dirty rows cleared. Waits for
    a fold already running: one takes 0.7 s for the busiest hour's urls and
    27.5 s for every job (production copy, 2026-11-26). A fold that skipped
    instead would drop the day's full fold whenever the cycle's fold won."""
    # Composed only of the fixed fragments above, never a value.
    statement = cast(
        "LiteralString",
        _FOLD.format(
            latest=_LATEST.format(jobs="" if everything else _DIRTY_JOBS),
            scope="TRUE" if everything else _SCOPE_DIRTY,
        ),
    )
    with pool.connection() as conn:
        conn.autocommit = True
        try:
            conn.execute(f"SELECT pg_advisory_lock({_LOCK_KEY})")
            try:
                counts = conn.execute(statement).fetchone()
            finally:
                conn.execute(f"SELECT pg_advisory_unlock({_LOCK_KEY})")
        finally:
            if not conn.closed:
                conn.autocommit = False
    assert counts is not None  # an aggregate with no GROUP BY is one row
    return {
        "written": counts["written"],
        "removed": counts["removed"],
        "cleared": counts["cleared"],
    }
