"""posting_embeddings, and the per-company text profiles they are embedded with

Revision ID: e3f1a7c95b20
Revises: d81c5e2b7a43
Create Date: 2026-11-26

A new table, not a change to job_embeddings: the width differs (768 against
1,536) and the similarity reader keeps reading job_embeddings until every
posting it covers has a vector here. The width is a frozen literal, as the
job_embeddings migration's is: a migration means the same thing forever, and
tests pin the live column to core.derivations.local_embeddings.

company_text_profiles and company_shared_paragraphs are the paragraphs each
company repeats across roles (core.derivations.company_text), derived and
rebuilt by the embedding run; a posting is embedded without them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

revision: str = "e3f1a7c95b20"
down_revision: str | None = "d81c5e2b7a43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "posting_embeddings",
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("embedding", HALFVEC(768), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("content_row_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


    op.create_table(
        "company_text_profiles",
        sa.Column("company_key", sa.Text(), primary_key=True),
        sa.Column("titles", sa.Integer(), nullable=False),
        sa.Column(
            "profiled_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "company_shared_paragraphs",
        sa.Column("company_key", sa.Text(), primary_key=True),
        sa.Column("paragraph_sha", sa.LargeBinary(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("company_shared_paragraphs")
    op.drop_table("company_text_profiles")
    op.drop_table("posting_embeddings")
