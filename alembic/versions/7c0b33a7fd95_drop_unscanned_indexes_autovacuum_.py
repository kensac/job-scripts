"""drop four indexes production never scanned; autovacuum review_gate_decisions

Revision ID: 7c0b33a7fd95
Revises: 507fe2f38949
Create Date: 2026-10-03

pg_stat_user_indexes on production, 2026-10-03, stats_reset NULL (the
counters have never been reset), idx_scan = 0 for each index dropped here:

    idx_ai_queries_job_title_trgm            195 MB
    idx_ai_queries_company_trgm               76 MB
    idx_ai_queries_cost_created               60 MB
    idx_review_gate_decisions_user_created    60 MB

391 MB, plus the write each insert paid into them. Every query that could
use one was checked, with EXPLAIN on a test database holding 1M rows of each
table at production's row width:

- cost_created is partial on cost_usd IS NOT NULL. No query on ai_queries
  has a WHERE that implies it (the spend filters on cost_usd read
  api_usage), so no plan could ever choose it.
- The company and job_title trigram indexes serve only the admin ledger
  searches (GET /admin/queries?q, GET /admin/jobs?q), a four-way OR over
  url, company, job_title and reason. The planner answers it with a BitmapOr
  over all four trigram indexes, which would have scanned these two; their
  zero means neither search ran in the life of the stats. Without them the
  OR cannot be index-backed: a parallel seq scan of ai_queries, 0.2 to
  1.6 s on the 1M-row copy against 1.3 to 2.3 ms. Accepted because the path
  is administrator-only and unused; the url and reason trigram indexes stay,
  for the fetch pacing LIKE and the reason-group regex that do scan them.
- user_created served the personal decision read (url AND user_id) and the
  administrator's user filter. The personal read uses url_created instead,
  1.2 ms on the copy. The administrator's ?user= count becomes a parallel
  seq scan, 0.4 to 0.6 s on the copy (1.6 GB), about six times that at
  production's 9.8 GB; the unfiltered administrator view already counts the whole table.

idx_review_gate_decisions_url_created (1431 MB) also shows zero scans and is
kept. GET /user/jobs/{id}/review-decisions reads by url from a person's job
drawer. Without it the planner walks the whole (task_id, url) unique index:
a skip scan, one descent per distinct task_id, 6 to 26 ms with 1,000 tasks
on the copy, and a full index walk at 100,000 tasks, 35 to 191 ms on the
copy and proportionally more over production's index.

Each drop is CONCURRENTLY, so ai_queries and review_gate_decisions keep
taking writes; it waits for transactions already using the table, and the
lock timeout turns a wait that will not end into a failed start that
retries, as in bd1e66f153c3.

Autovacuum on review_gate_decisions: the table is insert-only, 9.8 GB at
about 1.3 KB a row (7.4M rows), with pages about 95% full, and a backfill is
about to update every row. A full page has no room for a heap-only update,
so each new version goes wherever free space is recorded, and space is
recorded only when vacuum frees it. At the stock scale factor 0.2, about
1.48M dead versions (1.9 GB) accumulate before the first vacuum, all of
them written by extending the file. At 0.02 that is 148k (190 MB), the
same value ai_queries has carried since f3a4b5c6d7e8, after which the
backfill reuses what vacuum freed. The insert scale factor matches so the
visibility map that the url_created index-only count depends on is set every
148k inserts rather than every 1.48M, and analyze at 0.01 (74k rows) keeps
the planner's statistics within a day's writes. These settings take effect
at autovacuum's next check: SET takes SHARE UPDATE EXCLUSIVE, which blocks
neither reads nor writes, and nothing is rewritten.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c0b33a7fd95"
down_revision: str | None = "507fe2f38949"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROPPED = {
    "idx_ai_queries_job_title_trgm": "ai_queries USING gin (job_title gin_trgm_ops)",
    "idx_ai_queries_company_trgm": "ai_queries USING gin (company gin_trgm_ops)",
    "idx_ai_queries_cost_created": "ai_queries (created_at) WHERE cost_usd IS NOT NULL",
    "idx_review_gate_decisions_user_created": "review_gate_decisions (user_id, created_at)",
}
_AUTOVACUUM = {
    "autovacuum_vacuum_scale_factor": "0.02",
    "autovacuum_vacuum_insert_scale_factor": "0.02",
    "autovacuum_analyze_scale_factor": "0.01",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # bd1e66f153c3's judgment: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        op.execute(
            "ALTER TABLE review_gate_decisions SET ("
            + ", ".join(f"{key} = {value}" for key, value in _AUTOVACUUM.items())
            + ")"
        )
        for name in _DROPPED:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        for name, body in _DROPPED.items():
            # An interrupted build leaves an INVALID index that IF NOT EXISTS
            # would accept as finished.
            invalid = op.get_bind().execute(
                sa.text(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": name},
            )
            if invalid.first():
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {body}")
        op.execute("ALTER TABLE review_gate_decisions RESET (" + ", ".join(_AUTOVACUUM) + ")")
        op.execute("RESET lock_timeout")
