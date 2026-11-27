"""page_text_rejections: page text that is not its posting's

A fetch can bring back a page that is not the posting: Workday's maintenance
notice (saved for 1,304 postings across 113 Workday sites by 2026-11-27), a
career site's shell that renders without the posting, a cookie wall. Such a
fetch is still a true record of what the page said, and the closed check
still reads it (a closure page is evidence), so it is not marked failed.
What it is not is the posting's text. page_text_rejections names those
fetches (core.page_fetches, tasks.page_text_judging), and page_texts leaves
them out, so every reader of a posting's text falls back to its newest fetch
that is its own (CONTENT_LATERAL is ORDER BY id DESC LIMIT 1 over the view).

page_fetch_texts is page_texts without that exclusion: what one fetch held,
for the readers that ask by id (what an answer read, what a run froze).

The sweep groups fetches by their text's digest, so content_sha256 gets an
index, built CONCURRENTLY: page_fetches holds 2.1M rows and every fetch
writes one. A concurrent build waits for every older transaction, so the
lock timeout turns a wait that will not end into a failed start that
retries, and an INVALID index an interrupted build left behind is dropped
first.

Revision ID: 0838a60ed6e9
Revises: a4c9e2f7b318
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0838a60ed6e9"
down_revision: str | None = "a4c9e2f7b318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_page_fetches_content_sha256"

# The columns and condition of page_texts as 7e5c9370784e left them; the
# view keeps both and gains the exclusion.
_SELECT = """
    SELECT id, url, content AS input_content, created_at,
           COALESCE(content_chars, length(content)) AS chars,
           CASE WHEN content_object IS NOT NULL THEN jsonb_build_object(
               'object', encode(content_object, 'hex'),
               'size', content_object_size,
               'member', encode(content_sha256, 'hex')) END AS content_ref
    FROM page_fetches
    WHERE (content <> '' OR content_object IS NOT NULL)
"""
_NOT_REJECTED = """
      AND NOT EXISTS (SELECT 1 FROM page_text_rejections r WHERE r.fetch_id = page_fetches.id)
"""


def upgrade() -> None:
    op.create_table(
        "page_text_rejections",
        sa.Column(
            "fetch_id",
            sa.BigInteger(),
            sa.ForeignKey("page_fetches.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "rejected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        # The index build below commits what precedes it, so a start that
        # times out on the build retries all of it: everything here applies
        # again over what it already made.
        if_not_exists=True,
    )
    op.execute(f"CREATE OR REPLACE VIEW page_texts AS {_SELECT} {_NOT_REJECTED}")
    op.execute(f"CREATE OR REPLACE VIEW page_fetch_texts AS {_SELECT}")
    with op.get_context().autocommit_block():
        # A judgment, not a measurement: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        invalid = sa.text(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND NOT i.indisvalid"
        )
        if op.get_bind().execute(invalid, {"name": _INDEX}).first():
            op.execute(f"DROP INDEX CONCURRENTLY {_INDEX}")
        op.create_index(
            _INDEX,
            "page_fetches",
            ["content_sha256"],
            postgresql_where=sa.text("content_sha256 IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX, table_name="page_fetches", postgresql_concurrently=True, if_exists=True
        )
    op.execute("DROP VIEW IF EXISTS page_fetch_texts")
    op.execute(f"CREATE OR REPLACE VIEW page_texts AS {_SELECT}")
    op.drop_table("page_text_rejections")
