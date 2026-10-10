"""usage copies take no default and may be NULL

Nothing has written ai_batches' token totals or job_embeddings' cost shares
since #923. tasks.usage_copies sets them to NULL so the next release can prove
them empty and drop them. Two things in the schema would refill them:
ai_batches.input_tokens and output_tokens are NOT NULL, and they and
job_embeddings.input_tokens default to 0, which a new row would take. This
removes both. Catalog only: no row is read or rewritten.

Revision ID: 859638ba54ca
Revises: 8aa6dcf6aede
Create Date: 2026-10-10 05:18:03.187817

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "859638ba54ca"
down_revision: str | None = "8aa6dcf6aede"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '10s'")
    for column in ("input_tokens", "output_tokens"):
        op.alter_column(
            "ai_batches", column, existing_type=sa.BIGINT(), nullable=True, server_default=None
        )
    op.alter_column(
        "job_embeddings", "input_tokens", existing_type=sa.Integer(), server_default=None
    )


def downgrade() -> None:
    op.alter_column(
        "job_embeddings", "input_tokens", existing_type=sa.Integer(), server_default=sa.text("0")
    )
    for column in ("input_tokens", "output_tokens"):
        op.execute(f"UPDATE ai_batches SET {column} = 0 WHERE {column} IS NULL")
        op.alter_column(
            "ai_batches",
            column,
            existing_type=sa.BIGINT(),
            nullable=False,
            server_default=sa.text("0"),
        )
