"""page_fetches: columns for page text held by reference

A fetch's text can be held by reference to a verified bundle object, as
listings' descriptions are (core.payloads.listings). This adds the shape and
nothing that writes it, so every reader on the fleet understands it before
anything moves (core.page_fetches.resolve, docs/agents/storage.md):

- content_sha256: SHA-256 of encode_payload(text), the member's name in its
  bundle; content_object: the bundle's SHA-256; content_object_size: its
  length. content is NULL on such a row, so a reader that skipped resolve
  fails on None rather than judging an empty page.
- content_chars: the text's length, so "longer than MIN_CONTENT_CHARS" stays
  answerable in SQL. NULL on an inline row, which measures content instead;
  storing it for all 1.4M rows would rewrite every row for nothing.

page_texts keeps every row with text either way, with `chars` and
`content_ref` added at the end (CREATE OR REPLACE may only append), and
idx_page_fetches_url_text is rebuilt on the view's new conditions so
CONTENT_LATERAL's id lookup stays index-only. The rebuild is CONCURRENTLY
under a staged name, then swapped in by RENAME (SHARE UPDATE EXCLUSIVE), so
writers never wait behind it.

Revision ID: 7e5c9370784e
Revises: 485340937d5f
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7e5c9370784e"
down_revision: str | None = "485340937d5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "idx_page_fetches_url_text"
_STAGED = "idx_page_fetches_url_text_staged"
_HAS_TEXT = "(content <> '' OR content_object IS NOT NULL)"
_CHARS = "COALESCE(content_chars, length(content))"
_NEW_PREDICATE = f"{_HAS_TEXT} AND {_CHARS} > 200"
_OLD_PREDICATE = "content <> '' AND length(content) > 200"
_GUARD = "ck_page_fetches_reference_whole"

_VIEW = f"""
    CREATE OR REPLACE VIEW page_texts AS
    SELECT id, url, content AS input_content, created_at,
           {_CHARS} AS chars,
           CASE WHEN content_object IS NOT NULL THEN jsonb_build_object(
               'object', encode(content_object, 'hex'),
               'size', content_object_size,
               'member', encode(content_sha256, 'hex')) END AS content_ref
    FROM page_fetches
    WHERE {_HAS_TEXT}
"""
_OLD_VIEW = """
    CREATE VIEW page_texts AS
    SELECT id, url, content AS input_content, created_at
    FROM page_fetches
    WHERE content IS NOT NULL AND content <> ''
"""


def _invalid(name: str) -> bool:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": name},
        )
        .first()
        is not None
    )


def _swap(predicate: str) -> None:
    if _invalid(_STAGED):
        op.execute(f"DROP INDEX CONCURRENTLY {_STAGED}")
    op.execute(
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_STAGED} "
        f"ON page_fetches (url, id DESC) WHERE {predicate}"
    )
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    op.execute(f"ALTER INDEX {_STAGED} RENAME TO {_INDEX}")


def upgrade() -> None:
    op.add_column("page_fetches", sa.Column("content_sha256", sa.LargeBinary(), nullable=True))
    op.add_column("page_fetches", sa.Column("content_object", sa.LargeBinary(), nullable=True))
    op.add_column("page_fetches", sa.Column("content_object_size", sa.Integer(), nullable=True))
    op.add_column("page_fetches", sa.Column("content_chars", sa.Integer(), nullable=True))
    # Catalog only on a table where every row has the new columns NULL.
    op.execute(
        f"ALTER TABLE page_fetches ADD CONSTRAINT {_GUARD} CHECK ("
        "(content_object IS NULL AND content_sha256 IS NULL "
        " AND content_object_size IS NULL AND content_chars IS NULL) OR "
        "(content IS NULL AND content_sha256 IS NOT NULL "
        " AND content_object_size IS NOT NULL AND content_chars IS NOT NULL)) NOT VALID"
    )
    op.execute(_VIEW)
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        op.execute(f"ALTER TABLE page_fetches VALIDATE CONSTRAINT {_GUARD}")
        _swap(_NEW_PREDICATE)
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    # Dropping the columns would lose every text held by reference. Bring
    # them back inline first.
    held = op.get_bind().execute(
        sa.text("SELECT count(*) FROM page_fetches WHERE content_object IS NOT NULL")
    )
    if held.scalar():
        raise RuntimeError("page_fetches still holds text by reference; inline it first")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        _swap(_OLD_PREDICATE)
        op.execute("RESET lock_timeout")
    op.execute(f"ALTER TABLE page_fetches DROP CONSTRAINT {_GUARD}")
    op.execute("DROP VIEW page_texts")
    op.execute(_OLD_VIEW)
    for column in ("content_chars", "content_object_size", "content_object", "content_sha256"):
        op.drop_column("page_fetches", column)
