"""drop two ai_queries indexes nothing reads any more

Revision ID: 485340937d5f
Revises: c3d9a1f5e8b2
Create Date: 2026-10-31

Production, 2026-10-31 (stats_reset NULL):

    idx_ai_queries_url_trgm    660 MB    882 scans, last 2026-10-16
    idx_ai_queries_url         165 MB    107M scans, last today

The trigram index served one query: the fetch pacing count,
`(url LIKE 'https://host/%' OR url LIKE 'http://host/%')` over the last hour
(api/ai/verdicts.py). That count reads page_fetches since page text moved
out of ai_queries, which is when the scans stopped. The admin ledger search
cannot use it: its OR also covers company and job_title, which have no
trigram index (7c0b33a7fd95), so it is a sequential scan with or without it.

idx_ai_queries_url is the leading column of idx_ai_queries_url_check
(url, check_type), which answers every url lookup it answered. EXPLAIN
ANALYZE on the 2026-10-05 copy of production with both dropped inside a
rolled-back transaction: the verdict-cache reads keep idx_ai_queries_latest_*,
the url = ANY(...) reads move to idx_ai_queries_url_check at the same
timing (7 to 142 ms for 200 urls), and the old fetch count would use
idx_ai_queries_created_at.

825 MB of the database's 21 GB, and one index write fewer on every insert,
about 8 MB a day of growth.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "485340937d5f"
down_revision: str | None = "c3d9a1f5e8b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROPPED = {
    "idx_ai_queries_url_trgm": "ai_queries USING gin (url gin_trgm_ops)",
    "idx_ai_queries_url": "ai_queries (url)",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
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
        op.execute("RESET lock_timeout")
