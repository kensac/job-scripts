"""Board membership: computed by a task, read as a lookup.

Which postings are on a person's board is a question whose answer changes
when a preference changes or a verdict lands, not on every read. It used to
be answered on every read, by FULL below, which runs the closed and clearance
verdicts, the filter passes and the location and date criteria over every
posting on the person's boards: 2 to 7 seconds a sort on Kanishk's board,
twice when the total rode along. Now a worker runs FULL once per person and
stores the ids in `board_visible`; the board reads FAST, which is a lookup.

Freshness is minutes, not a day: a preference write asks for a recompute at
once (request_refresh), and the scheduler asks for every person every
`board_refresh_minutes` so new verdicts arrive on their own. A posting the
person uploaded or acted on (a status, a note, a date applied) is visible
without waiting; it is theirs whatever the computation says.

FULL is the one spelling of the predicate; the task and nothing else runs
it. FAST is the one spelling of the read; every per-object route and the
requirements slice go through it.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from api import criteria, db

logger = logging.getLogger("jobtracker_api")

FULL = """
WITH enabled_filters AS (
    -- DISTINCT is load-bearing: two enabled filters can share a prompt_hash
    -- (same prompt text under different names - adopting a preset and then
    -- pasting the same prompt does it). filter_pass dedupes per hash, so
    -- without this the passed_count could never reach COUNT(*) and the board
    -- would silently go empty.
    SELECT DISTINCT prompt_hash FROM user_filters WHERE user_id = %(uid)s AND enabled
),
latest_check AS (
    SELECT DISTINCT ON (url, check_type) url, check_type, status
    FROM ai_queries
    WHERE check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')
    ORDER BY url, check_type, id DESC
),
filter_pass AS (
    -- The hashes go in as an array, not a join. Joined, the planner sorted
    -- 26,000 (url, hash) rows under the locale collation before the DISTINCT
    -- ON, 320 ms of a 365 ms table; as an array the index-only scan on
    -- idx_ai_queries_latest_custom already yields index order and there is
    -- no sort at all: 28 ms, measured on production 2026-09-05.
    SELECT url, COUNT(*) AS passed_count FROM (
        SELECT DISTINCT ON (q.url, q.prompt_hash) q.url, q.status
        FROM ai_queries q
        WHERE q.check_type = 'custom' AND q.status IN ('passed', 'rejected')
          AND q.prompt_hash = ANY(ARRAY(SELECT prompt_hash FROM enabled_filters))
        ORDER BY q.url, q.prompt_hash, q.id DESC
    ) t WHERE t.status = 'passed' GROUP BY url
)
SELECT {columns}
FROM jobs j
LEFT JOIN user_jobs uj ON uj.job_id = j.id AND uj.user_id = %(uid)s
-- Joined once, not looked up per row: as a correlated subquery this ran
-- 13,566 times per board read.
LEFT JOIN filter_pass fp ON fp.url = j.url
WHERE (
    j.uploaded_by = %(uid)s
    -- A board row the person ACTED on (a status, a note, a date applied) is
    -- theirs whatever the criteria say. A row the worker materialised for a
    -- passing posting and nobody touched is not a decision, so it obeys the
    -- criteria like any other posting: 1,629 such rows carried postings in
    -- Singapore, London and Sydney past a United States filter on
    -- 2026-09-05, because a row's mere existence read as a grant.
    OR (uj.user_id IS NOT NULL
        AND (COALESCE(uj.status, '') <> '' OR COALESCE(uj.notes, '') <> ''
             OR uj.date_applied IS NOT NULL
             OR (TRUE {criteria})))
    OR (
        j.active
        AND j.source IN (SELECT source FROM user_sources WHERE user_id = %(uid)s)
        {criteria}
        AND EXISTS (SELECT 1 FROM latest_check lc
                    WHERE lc.url = j.url AND lc.check_type = 'closed' AND lc.status = 'passed')
        AND (%(bypass_sponsorship)s
             OR EXISTS (SELECT 1 FROM latest_check lc
                        WHERE lc.url = j.url AND lc.check_type = 'clearance' AND lc.status = 'passed'))
        AND ((SELECT COUNT(*) FROM enabled_filters) = 0
             OR COALESCE(fp.passed_count, 0)
                = (SELECT COUNT(*) FROM enabled_filters))
    )
)
{extra}
"""

FAST = """
SELECT {columns}
FROM jobs j
LEFT JOIN user_jobs uj ON uj.job_id = j.id AND uj.user_id = %(uid)s
WHERE (
    j.uploaded_by = %(uid)s
    OR (uj.user_id IS NOT NULL
        AND (COALESCE(uj.status, '') <> '' OR COALESCE(uj.notes, '') <> ''
             OR uj.date_applied IS NOT NULL))
    OR EXISTS (SELECT 1 FROM board_visible bv WHERE bv.user_id = %(uid)s AND bv.job_id = j.id)
)
{extra}
"""


def settings_params(user_id: int) -> dict[str, Any]:
    settings = db.query_one(
        "SELECT bypass_sponsorship_filter, criteria FROM user_settings WHERE user_id = %s",
        (user_id,),
    )
    return {
        "uid": user_id,
        "bypass_sponsorship": settings["bypass_sponsorship_filter"] if settings else True,
        **criteria.params(settings),
    }


def member_ids(user_id: int) -> list[int]:
    """Every job the full predicate admits for this person, right now."""
    rows = db.query(
        FULL.format(columns="j.id", extra="", criteria=criteria.SQL), settings_params(user_id)
    )
    return [r["id"] for r in rows]


def recompute(user_id: int) -> int:
    """Replace the person's membership with the full predicate's answer, in
    one transaction, so a read never sees the board half-built."""
    ids = member_ids(user_id)
    with db.pool.connection() as conn:
        # One recompute per person at a time. Two ran together on 2026-09-05
        # (the three-minute cycle beside a preference-driven refresh) and the
        # second died on a duplicate key between the other's DELETE and INSERT.
        # The lock is transaction-scoped, so the later one waits and then
        # recomputes on top of the earlier result rather than under it.
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (7001, user_id))
        conn.execute("DELETE FROM board_visible WHERE user_id = %s", (user_id,))
        conn.execute(
            "INSERT INTO board_visible (user_id, job_id, computed_at) "
            "SELECT %s, unnest(%s::bigint[]), now()",
            (user_id, ids),
        )
    return len(ids)


def computed_at(user_id: int) -> datetime.datetime | None:
    row = db.query_one(
        "SELECT MAX(computed_at) AS at FROM board_visible WHERE user_id = %s", (user_id,)
    )
    return row["at"] if row else None


def request_refresh(user_id: int) -> None:
    """Ask for a recompute soon: at most one task per person per minute,
    so a burst of preference edits is one recompute, and the fleet's five
    second poll is the latency."""
    from api.tasks.runtime import enqueue

    bucket = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M")
    enqueue("recompute_board", {"user_id": user_id}, dedupe_key=f"board:{user_id}:{bucket}")
