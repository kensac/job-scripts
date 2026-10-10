"""drop ai_experiments and ai_experiment_results

Revision ID: 93a3d7d0c92f
Revises: 6ba27711ec9e
Create Date: 2026-10-10

The contract half of removing the admin experiments path. The release before
this one (#876) removed the router and the task, the only code that named
these tables; experiments run from `api.run_experiment` and keep their
answers in files. Production held 5 runs and 3,240 results (2.3 MB, last
written 2026-09-07), exported before the drop.

DROP TABLE takes ACCESS EXCLUSIVE on two tables nothing reads, so it waits
on nobody.

Downgrade recreates both tables empty.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "93a3d7d0c92f"
down_revision: Union[str, None] = "6ba27711ec9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("ai_experiment_results")
    op.drop_table("ai_experiments")


def downgrade() -> None:
    op.create_table(
        "ai_experiments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("summary", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
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
