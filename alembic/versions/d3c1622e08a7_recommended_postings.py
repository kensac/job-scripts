"""recommended_postings and recommendation_runs: a person's For you ranking

The For you page held a person's taste ranking to their board's conditions
(criteria, and no rejection from the shared checks or their enabled filters)
and folded a job's copies into one row. Evaluated on each read that cost 6.8 s
for a page and 6.6 s for its copies (production, warm, 2026-11-27), so it is
computed in a task, as board membership is (docs/agents/visibility.md), and
read from here.

recommendation_runs holds each person's latest run: the model it ranked with
and when. recommended_postings holds that run's postings: score, rank, whether
every enabled filter kept it, and the posting it is a near copy of, when one
ranks above it.

Revision ID: d3c1622e08a7
Revises: 4152de11ff78
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3c1622e08a7"
down_revision: str | None = "4152de11ff78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_runs",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "model_id",
            sa.BigInteger(),
            sa.ForeignKey("ranking_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("postings", sa.Integer(), nullable=False),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.create_table(
        "recommended_postings",
        sa.Column(
            "user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("kept", sa.Boolean(), nullable=False),
        sa.Column("copy_of", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("user_id", "job_id"),
    )
    op.create_index(
        "idx_recommended_postings_rows",
        "recommended_postings",
        ["user_id", "rank"],
        postgresql_where=sa.text("copy_of IS NULL"),
    )
    op.create_index(
        "idx_recommended_postings_copies", "recommended_postings", ["user_id", "copy_of"]
    )


def downgrade() -> None:
    op.drop_table("recommended_postings")
    op.drop_table("recommendation_runs")
