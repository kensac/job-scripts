"""jobs.near_copy_key: one company's text posted at several places

verify_new reuses a verified twin's verdicts for a posting whose source,
title and text (location lines and numbers removed) match it, instead of
paying to read the same text again (core.near_copy). 16.4% of postings
verified 2026-10-06 to 10-08 had such a twin, and in 636 twin groups the
boards' verdicts agreed in 635 and 636, closed and clearance in 634.

The column is nullable and written by verify_new, so adding it is a catalog
change only. The index is (source, near_copy_key) for the twin lookup, built
CONCURRENTLY under a lock timeout, an INVALID leftover dropped first, as
1043e0d541ff does.

Revision ID: ce29f52b5f6b
Revises: 5dc82667db3b
Create Date: 2026-10-09 05:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ce29f52b5f6b"
down_revision: str | None = "5dc82667db3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_jobs_near_copy"


def upgrade() -> None:
    op.add_column("jobs", sa.Column("near_copy_key", sa.Text(), nullable=True))
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        invalid = op.get_bind().execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": _INDEX},
        )
        if invalid.first():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.create_index(
            _INDEX,
            "jobs",
            ["source", "near_copy_key"],
            postgresql_concurrently=True,
            postgresql_where=sa.text("near_copy_key IS NOT NULL"),
            if_not_exists=True,
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        op.drop_index(_INDEX, table_name="jobs", postgresql_concurrently=True, if_exists=True)
        op.execute("RESET lock_timeout")
    op.drop_column("jobs", "near_copy_key")
