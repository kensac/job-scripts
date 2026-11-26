"""Bring latest_posting_checks to what `verdicts` says.

The triggers on ai_queries and jobs keep the projection exact as answers and
postings are written (migration d81c5e2b7a43). This is the other half: the
first fill, and a check that finds nothing to do while the triggers hold. A
write they cannot see (triggers disabled, `session_replication_role =
replica`, a TRUNCATE) is caught here, at the next run.

Two set-based statements in one transaction, each taking the rows it writes
in (job_id, check_type) order, the order the triggers take them in:
- fill: the latest answer per (job, check) where the stored one is missing
  or older; a newer stored answer is kept whatever this snapshot saw;
- repair: a stored answer that no longer exists (deleted while a trigger
  raced it) is replaced by the key's latest answer, or removed when it has
  none. Only answers at or below the highest id this snapshot can see: an
  answer committed after the snapshot is newer, not missing.
"""

from __future__ import annotations

from typing import LiteralString

from core.pool import connection, transaction

# Every check `verdicts` holds but custom filters, which are keyed by prompt.
# By exclusion, as the triggers select, so a new posting check needs no change.
_LATEST = """
    SELECT DISTINCT ON (j.id, v.check_type) j.id AS job_id, v.check_type,
           v.id AS verdict_id, v.status
    FROM verdicts v JOIN jobs j ON j.url = v.url
    WHERE v.check_type <> 'custom'
    ORDER BY j.id, v.check_type, v.id DESC
"""

_FILL: LiteralString = f"""
    INSERT INTO latest_posting_checks (job_id, check_type, verdict_id, status)
    SELECT job_id, check_type, verdict_id, status FROM ({_LATEST}) latest
    ORDER BY job_id, check_type
    ON CONFLICT (job_id, check_type) DO UPDATE
      SET verdict_id = EXCLUDED.verdict_id, status = EXCLUDED.status
      WHERE latest_posting_checks.verdict_id < EXCLUDED.verdict_id
"""

_REPAIR: LiteralString = """
    WITH horizon AS MATERIALIZED (SELECT max(id) AS id FROM ai_queries),
    stale AS MATERIALIZED (
        SELECT l.job_id, l.check_type FROM latest_posting_checks l
        WHERE l.verdict_id <= (SELECT id FROM horizon)
          AND NOT EXISTS (SELECT 1 FROM ai_queries a WHERE a.id = l.verdict_id)
    ), fresh AS MATERIALIZED (
        SELECT s.job_id, s.check_type, v.id AS verdict_id, v.status
        FROM stale s JOIN jobs j ON j.id = s.job_id
        LEFT JOIN LATERAL (
            SELECT vl.id, vl.status FROM verdicts vl
            WHERE vl.url = j.url AND vl.check_type = s.check_type
            ORDER BY vl.id DESC LIMIT 1) v ON TRUE
    ), gone AS (
        DELETE FROM latest_posting_checks l USING fresh f
        WHERE l.job_id = f.job_id AND l.check_type = f.check_type
          AND f.verdict_id IS NULL
        RETURNING 1
    ), replaced AS (
        INSERT INTO latest_posting_checks (job_id, check_type, verdict_id, status)
        SELECT job_id, check_type, verdict_id, status FROM fresh
        WHERE verdict_id IS NOT NULL
        ORDER BY job_id, check_type
        ON CONFLICT (job_id, check_type) DO UPDATE
          SET verdict_id = EXCLUDED.verdict_id, status = EXCLUDED.status
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM gone) AS removed, (SELECT count(*) FROM replaced) AS replaced
"""


def reconcile() -> dict[str, int]:
    """Fill and repair; the counts of rows each step wrote. All zero when the
    projection already matched."""
    with transaction(), connection() as conn:
        filled = conn.execute(_FILL).rowcount
        row = conn.execute(_REPAIR).fetchone()
    assert row is not None  # an aggregate with no GROUP BY is one row
    return {"filled": filled, "removed": row["removed"], "replaced": row["replaced"]}
