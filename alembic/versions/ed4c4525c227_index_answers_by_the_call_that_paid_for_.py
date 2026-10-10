"""ai_queries: index answers by the call that paid for them

One call can answer several checks, and the first answer naming it carries
its usage (docs/agents/architecture-migration.md, phase 8). The readers that
switch to the call's numbers find that answer per call, min(id) grouped by
model_call_id, which this partial index serves as an index-only scan instead
of reading the 1.4 GB heap: measured on production on 2026-10-10, that
aggregate took 5.1 s as a sequential scan. No reader uses it yet.

Built CONCURRENTLY, as in bd1e66f153c3, so writers to the live table never
wait behind the build. An interrupted build leaves an INVALID index that IF
NOT EXISTS would accept, so one is dropped first.

Revision ID: ed4c4525c227
Revises: dfd80983deba
Create Date: 2026-10-10 21:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ed4c4525c227"
down_revision: str | None = "dfd80983deba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INVALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = 'idx_ai_queries_model_call' AND NOT i.indisvalid"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # A judgment, not a measurement: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        if op.get_bind().execute(_INVALID).first():
            op.execute("DROP INDEX CONCURRENTLY idx_ai_queries_model_call")
        op.create_index(
            "idx_ai_queries_model_call",
            "ai_queries",
            ["model_call_id", "id"],
            unique=False,
            postgresql_where=sa.text("model_call_id IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_ai_queries_model_call",
            table_name="ai_queries",
            postgresql_concurrently=True,
            if_exists=True,
        )
