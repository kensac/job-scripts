"""page_fetches: index of the rows whose text is still inline

Revision ID: c5a1f7e9d203
Revises: c7e2a4f19d50
Create Date: 2026-11-26

A fetch's text is uploaded to object storage before its row is written
(core.page_fetches.record). Text is inline only on rows written before that,
or while storage was unavailable, and tasks.page_text_tiering moves those.
This partial index is how the move finds them: on 2026-11-26 1,259,274 of
2,093,537 fetches were inline, and once moved the index holds only what an
outage leaves. Its predicate is tasks.page_text_tiering.INLINE, spelled the
same way. Without it, once most rows have moved, every step walks the primary
key past each moved row to reach the next inline one.

Built CONCURRENTLY so fetch writes never wait on it, after dropping an
INVALID index an interrupted build left behind (IF NOT EXISTS would accept
it), under a lock timeout so a build that waits on a long transaction fails
the start and retries rather than hanging (bd1e66f153c3).

The move rewrites over a million rows, each update leaving its old version
and the old TOAST chunks dead. Stock autovacuum waits for a fifth of the
table, about 400k rows here, before reclaiming any of it, while every new
version extends the file. The scale factors match the other tables that take
bulk updates (jobs, listings, batch_requests, ai_queries), for the heap and
its TOAST table. ALTER TABLE ... SET takes SHARE UPDATE EXCLUSIVE and
rewrites nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5a1f7e9d203"
down_revision: str | None = "c7e2a4f19d50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_page_fetches_inline_text"
_INVALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = :name AND NOT i.indisvalid"
)
_AUTOVACUUM = (
    "autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.01, "
    "toast.autovacuum_vacuum_scale_factor = 0.02"
)
_AUTOVACUUM_KEYS = (
    "autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, "
    "toast.autovacuum_vacuum_scale_factor"
)


def upgrade() -> None:
    op.execute(f"ALTER TABLE page_fetches SET ({_AUTOVACUUM})")
    with op.get_context().autocommit_block():
        # A judgment, not a measurement, as in bd1e66f153c3: a start can afford
        # a minute for the transactions in flight, and past that the wait is
        # not ending.
        op.execute("SET lock_timeout = '60s'")
        if op.get_bind().execute(_INVALID, {"name": _INDEX}).first():
            op.execute(f"DROP INDEX CONCURRENTLY {_INDEX}")
        op.create_index(
            _INDEX,
            "page_fetches",
            ["id"],
            unique=False,
            postgresql_where=sa.text("content <> ''"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX, table_name="page_fetches", postgresql_concurrently=True, if_exists=True
        )
    op.execute(f"ALTER TABLE page_fetches RESET ({_AUTOVACUUM_KEYS})")
