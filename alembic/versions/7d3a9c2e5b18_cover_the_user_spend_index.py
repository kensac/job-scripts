"""cover the user spend index

idx_model_calls_user_created gains the columns a person's spend reads in an
INCLUDE list, and becomes partial on user_id IS NOT NULL. /user/usage and
/admin/users/{id} run model_calls.user_spend_by_purpose, which aggregates
every call the person ever made. For the owner that is 609,567 rows on ~37,000
heap pages of a 577 MB table: on production (2026-11-01) it took 4.2 s with
20,611 pages read from disk. With the columns in the index the scan is
index-only. The visibility map marks 89% of the table all-visible.

Partial because only reads by a person use this index. The fleet ceiling's
`user_id IS NULL` read uses idx_model_calls_created (EXPLAIN on production,
2026-11-01). 635k of 1.97M rows have a user, so the covered index is about the
size of the 75 MB one it replaces.

Rebuilt under a staged name and renamed over the old one, as in
0b6e2f1c9a47.

Revision ID: 7d3a9c2e5b18
Revises: 0b6e2f1c9a47
Create Date: 2026-11-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d3a9c2e5b18"
down_revision: str | None = "0b6e2f1c9a47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "idx_model_calls_user_created"
_PLAIN = "model_calls (user_id, created_at)"
_COVERED = (
    "model_calls (user_id, created_at) INCLUDE (purpose, model, key_source, total_tokens, "
    "requests, cost_usd, cached_tokens, cache_write_tokens) WHERE user_id IS NOT NULL"
)


def _rebuild(on: str) -> None:
    bind = op.get_bind()
    definition = (
        bind.execute(
            sa.text("SELECT pg_get_indexdef(to_regclass(:name)::oid)"), {"name": _NAME}
        ).scalar()
        or ""
    )
    if ("INCLUDE" in definition) == ("INCLUDE" in on):
        return
    staged = f"{_NAME}_next"
    invalid = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND NOT i.indisvalid"
        ),
        {"name": staged},
    )
    if invalid.first():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {staged}")
    op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {staged} ON {on}")
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NAME}")
    op.execute(f"ALTER INDEX {staged} RENAME TO {_NAME}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        _rebuild(_COVERED)
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        _rebuild(_PLAIN)
        op.execute("RESET lock_timeout")
