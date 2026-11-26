"""posting_embedding_failures: pages the embedding servers refused

Revision ID: b7d3f1a9c264
Revises: a4c9e2f7b318
Create Date: 2026-11-26

The sweep now walks every posting, and a page that fails on every try would
otherwise be selected again by every batch, ahead of the postings behind it.
A failure is recorded against the page (content_row_id), so the same page is
not tried again and a newer one is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d3f1a9c264"
down_revision: str | None = "a4c9e2f7b318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "posting_embedding_failures",
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("content_row_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "failed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("posting_embedding_failures")
