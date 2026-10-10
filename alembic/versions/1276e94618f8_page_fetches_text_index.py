"""page_fetches: index the fetches that hold usable text, by url

Two readers ask "which fetch of this url holds text longer than
MIN_CONTENT_CHARS": the content backfill's gap check (tasks/content.py, a
NOT EXISTS per active job) and CONTENT_LATERAL's id lookup in the
derivation sweeps. With only (url, id DESC) each reads every fetch of the
url from the heap and measures its text, which is TOASTed. This index holds
exactly the rows they want, so both are index-only scans.

The predicate repeats page_texts' `content <> ''`: the planner drops a
condition the predicate implies, and it cannot prove `content <> ''` from
`length(content) > 200`, so without it every match goes back to the heap.

Built CONCURRENTLY, as in bd1e66f153c3, so writers to the live table never
wait behind the build. An interrupted build leaves an INVALID index that IF
NOT EXISTS would accept, so one is dropped first.

Revision ID: 1276e94618f8
Revises: 213749d456b4
Create Date: 2026-10-10 18:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1276e94618f8"
down_revision: str | None = "213749d456b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INVALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = 'idx_page_fetches_url_text' AND NOT i.indisvalid"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # A judgment, not a measurement: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        if op.get_bind().execute(_INVALID).first():
            op.execute("DROP INDEX CONCURRENTLY idx_page_fetches_url_text")
        op.create_index(
            "idx_page_fetches_url_text",
            "page_fetches",
            ["url", sa.literal_column("id DESC")],
            unique=False,
            postgresql_where=sa.text("content <> '' AND length(content) > 200"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_page_fetches_url_text",
            table_name="page_fetches",
            postgresql_concurrently=True,
            if_exists=True,
        )
