"""page_texts: the rows of ai_queries that hold a posting's page text

Page text is stored two ways. A fetch writes a row of its own (check_type
'content'). Verification before those rows existed wrote the text only onto
its closed and clearance answers; 11,142 urls on 2026-10-09 have text only
there. A custom filter's row holds the page wrapped with the company and
title, which is its input, not the page.

Two readers disagreed about that. get_contents excluded custom rows and
CONTENT_LATERAL did not, so for the 15 urls whose only text is a custom
input the sweeps read the wrapped text as the page. The view states the rule
once, and `on_verdict` says which copies are the old kind, so step 3 of the
split (docs/agents/architecture-migration.md, phase 8) can move them to page
rows and the column then reads false everywhere.

Creating it takes ACCESS SHARE on ai_queries only.

Revision ID: b3e9f2a41c77
Revises: a7d1e5c90b31
Create Date: 2026-10-09 23:55:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b3e9f2a41c77"
down_revision: str | None = "a7d1e5c90b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW page_texts AS
        SELECT id, url, input_content, created_at,
               check_type <> 'content' AS on_verdict
        FROM ai_queries
        WHERE check_type <> 'custom'
          AND input_content IS NOT NULL AND input_content <> ''
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS page_texts")
