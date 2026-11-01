"""cover the current match index

idx_email_messages_current_match gains current_event_id, sent_at, from_email
and subject in an INCLUDE list. The pipeline list and summary, the funnel and
the sender signal all walk every matched message through this index and then
read those columns from the heap. email_messages rows carry the mail bodies,
so the 9,809 matched rows sit on ~5,800 heap pages. On production
(2026-11-01) a cold read of them was 5.4 s of the pipeline's events query and
4.8 s of the funnel's 6.1 s; warm, the same queries take 94 ms. The visibility
map already marks almost every page all-visible (76 heap fetches in 9,809 on
the index-only plan that reads current_match_id alone), so with the columns in
the index both scans become index-only. The index is ~400 kB today and about
1.5 MB with the INCLUDE list.

Rebuilt under a staged name and renamed over the old one, as in
1afab52e064c: the build is CONCURRENT so writers continue, an INVALID
leftover from a build that died is dropped first, and a start that died after
the rename finds the definition already in place and skips the rebuild.

Revision ID: 0b6e2f1c9a47
Revises: 255b3de2e3a3
Create Date: 2026-11-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0b6e2f1c9a47"
down_revision: str | None = "255b3de2e3a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "idx_email_messages_current_match"
_ON = "email_messages (current_match_id){include} WHERE current_match_id IS NOT NULL"
_COVERED = " INCLUDE (current_event_id, sent_at, from_email, subject)"


def _rebuild(include: str) -> None:
    bind = op.get_bind()
    definition = (
        bind.execute(
            sa.text("SELECT pg_get_indexdef(to_regclass(:name)::oid)"), {"name": _NAME}
        ).scalar()
        or ""
    )
    if ("INCLUDE" in definition) == bool(include):
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
    op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {staged} ON {_ON.format(include=include)}")
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
        _rebuild("")
        op.execute("RESET lock_timeout")
