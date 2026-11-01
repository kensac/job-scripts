"""ai_queries: covering indexes for the filter insight window

The filter insight pages count a window of custom verdicts and read its
reasoned rejections. Through the heap the 30-day admin page took 23 to 37 s
on production (2026-11-01): 740,000 verdicts spread over 1.5 GB. These two
partial indexes carry every column those statements read, so both are
index-only scans. ai_queries's visibility map is kept all-visible by its
per-table autovacuum settings (200,227 of 200,321 pages on 2026-11-01).

Sizes from the column widths on production (2026-11-01): about 230 MB for the
1.48M custom verdicts and 23 MB for the 104,000 reasoned rejections. Each
build reads the heap; a full pass measured 8.3 s.

Built CONCURRENTLY so writes to ai_queries continue during the build; see
bd1e66f153c3 for the lock timeout and the INVALID index left by an
interrupted build.

Revision ID: 255b3de2e3a3
Revises: 4c366382d721
Create Date: 2026-11-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "255b3de2e3a3"
down_revision: Union[str, None] = "4c366382d721"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = {
    "idx_ai_queries_custom_window": (
        ["prompt_hash", "status", "url", "filter_name"],
        "check_type = 'custom'",
    ),
    "idx_ai_queries_custom_reasons": (
        ["prompt_hash", "url", "reason"],
        "check_type = 'custom' AND status = 'rejected' AND reason <> ''",
    ),
}


def _invalid(name: str) -> sa.TextClause:
    return sa.text(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        f"WHERE c.relname = '{name}' AND NOT i.indisvalid"
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # A judgment, as in bd1e66f153c3: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        for name, (include, where) in _INDEXES.items():
            if op.get_bind().execute(_invalid(name)).first():
                op.execute(f"DROP INDEX CONCURRENTLY {name}")
            op.create_index(
                name,
                "ai_queries",
                ["created_at"],
                unique=False,
                postgresql_include=include,
                postgresql_where=sa.text(where),
                postgresql_concurrently=True,
                if_not_exists=True,
            )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.drop_index(
                name, table_name="ai_queries", postgresql_concurrently=True, if_exists=True
            )
