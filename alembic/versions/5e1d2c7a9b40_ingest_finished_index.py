"""ingest finished index

idx_tasks_ingest_finished serves the admin Pulls tab's window, the finished
pulls of the last N hours. Without it the window is a seq scan of tasks (304
MB, 554k rows) for about 9k pulls a day: 0.37 s warm and 2.3 s cold on
production (2026-11-01). It covers 387k of the 554k rows.

Built CONCURRENTLY so ingest and the task claim keep writing, with an invalid
leftover dropped first and a lock timeout, as in c1746d710146.

Revision ID: 5e1d2c7a9b40
Revises: 58e4e8b6b54d
Create Date: 2026-11-01 01:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e1d2c7a9b40"
down_revision: str | None = "58e4e8b6b54d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "idx_tasks_ingest_finished"
_DEFINITION = (
    "tasks (finished_at) WHERE kind = 'ingest_source' AND status IN ('done', 'failed')"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        invalid = op.get_bind().execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": _NAME},
        )
        if invalid.first():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NAME}")
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_NAME} ON {_DEFINITION}")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NAME}")
        op.execute("RESET lock_timeout")
