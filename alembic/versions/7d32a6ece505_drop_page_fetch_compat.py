"""page_fetches: drop what kept older images working through the rename

213749d456b4 renamed page_fetch_rows to page_fetches and left a view
page_fetch_rows and an always-false page_texts.on_verdict for the images
still running during that roll. No code since names either, and every host
runs a release that does not, so both go.

Revision ID: 7d32a6ece505
Revises: 213749d456b4
Create Date: 2026-10-10 19:10:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "7d32a6ece505"
down_revision: str | None = "213749d456b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PAGE_TEXTS = """
    CREATE VIEW page_texts AS
    SELECT id, url, content AS input_content, created_at{extra}
    FROM page_fetches WHERE content IS NOT NULL AND content <> ''
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("DROP VIEW page_fetch_rows")
    op.execute("DROP VIEW page_texts")
    op.execute(_PAGE_TEXTS.format(extra=""))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("DROP VIEW page_texts")
    op.execute(_PAGE_TEXTS.format(extra=", false AS on_verdict"))
    op.execute("CREATE VIEW page_fetch_rows AS SELECT * FROM page_fetches")
