"""tasks: index on the managed board a run belongs to

Revision ID: bd1e66f153c3
Revises: 5f8904aaff79
Create Date: 2026-10-03

A board's latest run and the admission active-run check both read
(payload->>'managed_board_id')::bigint. Without an index each walked tasks
backwards decompressing payloads: 150,847 rows and 7.5 s for board 4 on
2026-10-03 (EXPLAIN ANALYZE). Partial on the two run kinds, like
idx_tasks_ingest_source, so it stays the size of the run history.

Built CONCURRENTLY so the build never blocks writes to the live tasks table
(220k rows): a plain build holds SHARE on tasks while it detoasts every
legacy run payload, and every heartbeat waits behind it. A concurrent build
waits for every transaction older than it instead, so the lock timeout turns
a waiter that will never finish into a failed start that retries, rather than
a hang. An interrupted build leaves an INVALID index that IF NOT EXISTS would
accept, so one is dropped first.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bd1e66f153c3"
down_revision: Union[str, None] = "5f8904aaff79"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INVALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = 'idx_tasks_managed_board' AND NOT i.indisvalid"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # A judgment, not a measurement: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        if op.get_bind().execute(_INVALID).first():
            op.execute("DROP INDEX CONCURRENTLY idx_tasks_managed_board")
        op.create_index(
            "idx_tasks_managed_board",
            "tasks",
            [
                sa.literal_column("((payload->>'managed_board_id')::bigint)"),
                sa.literal_column("id DESC"),
            ],
            unique=False,
            postgresql_where=sa.text("kind IN ('run_managed_board', 'run_managed_board_batch')"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_tasks_managed_board",
            table_name="tasks",
            postgresql_concurrently=True,
            if_exists=True,
        )
