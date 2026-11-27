"""ranking_models and posting_scopes: which postings are tech work

ranking_models holds a model fitted on posting embeddings: its weights, the
examples it was fitted on and how it scored on ones it did not see. The first
kind is the tech scope's classifier (core.tech_scope); a person's taste is
the next.

posting_scopes holds, for each embedded posting, whether it is tech work and
what decided: its title, or for a title that names technical work only
vaguely, its text through the scope model.

Revision ID: 14e68161d448
Revises: 835b21f59b79
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects import postgresql

revision: str = "14e68161d448"
down_revision: str | None = "835b21f59b79"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ranking_models",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("weights", HALFVEC(768), nullable=False),
        sa.Column("intercept", sa.Float(), nullable=False),
        sa.Column("examples", postgresql.JSONB(), nullable=False),
        sa.Column("evaluation", postgresql.JSONB(), nullable=False),
        sa.Column(
            "trained_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("kind IN ('tech_scope', 'taste')", name="ck_ranking_models_kind"),
    )
    op.create_index(
        "idx_ranking_models_latest",
        "ranking_models",
        ["kind", "user_id", sa.literal_column("id DESC")],
    )
    op.create_table(
        "posting_scopes",
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("in_scope", sa.Boolean(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column(
            "model_id",
            sa.BigInteger(),
            sa.ForeignKey("ranking_models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "scored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("decided_by IN ('title', 'text')", name="ck_posting_scopes_decided_by"),
    )


def downgrade() -> None:
    op.drop_table("posting_scopes")
    op.drop_index("idx_ranking_models_latest", table_name="ranking_models")
    op.drop_table("ranking_models")
