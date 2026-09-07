"""ai_experiments: a step measured across models and efforts on a sample

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-09-07

The nano-versus-luna filter comparison of 2026-09-06 was a hand-written
export and script, and the script fed every request the posting's first
line only; its headline number was wrong and was nearly acted on. A
measurement worth making is worth making through the production path:
the step's own instructions, schema and input, the same batch submission,
sampled by seed so it can be repeated. Two tables: the run and its arms,
and one row per arm and posting. Additive.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d4e5f6a7b8c9"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_experiments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        # {"sample": 100, "seed": "...", "arms": [{"model", "effort"}], "filter_id": ...}
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column(
            "created_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("summary", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "ai_experiment_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "experiment_id",
            sa.BigInteger(),
            sa.ForeignKey("ai_experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("arm", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("experiment_id", "arm", "url"),
    )


def downgrade() -> None:
    op.drop_table("ai_experiment_results")
    op.drop_table("ai_experiments")
