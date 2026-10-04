"""source ledger indexes

The admin source ledger reads each source's latest ingest task and its job
count and newest posting.

idx_tasks_ingest_source_latest serves the latest ingest as one LIMIT 1 probe
per source, ordered by id as the ledger always was. The existing
idx_tasks_ingest_source is ordered by created_at, which ties and need not
follow id, so it cannot answer "the last one" without a sort. On a test copy
at production scale (8,021 sources, 136,986 ingest tasks, 2026-10-04) the
DISTINCT ON over every ingest task took 370 ms with an external sort; the
probe takes 39 ms with this index and 103 s without it.

idx_jobs_source_created lets the per-source COUNT and MAX(created_at) read
the index alone: 1,110 buffers against 12,882 for a seq scan of 224,541 jobs
at production row width (same copy).

Both are built CONCURRENTLY so ingest and the task claim keep writing. A
build that died midway leaves an INVALID index that IF NOT EXISTS would
mistake for a finished one, so an invalid leftover is dropped first. A
concurrent build waits for every transaction older than itself; the lock
timeout turns a wait that will not end into a failed start that retries, as
in bd1e66f153c3 and 507fe2f38949.

Revision ID: c1746d710146
Revises: 7ca95d34ef5f
Create Date: 2026-10-03 22:12:31.274755

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1746d710146"
down_revision: str | None = "7ca95d34ef5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    (
        "idx_tasks_ingest_source_latest",
        "tasks ((payload->>'source'), id DESC) WHERE kind = 'ingest_source'",
    ),
    ("idx_jobs_source_created", "jobs (source, created_at)"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # bd1e66f153c3's judgment: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        for name, definition in _INDEXES:
            invalid = op.get_bind().execute(
                sa.text(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": name},
            )
            if invalid.first():
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {definition}")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        for name, _ in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute("RESET lock_timeout")
