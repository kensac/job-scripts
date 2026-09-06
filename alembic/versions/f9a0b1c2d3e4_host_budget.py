"""host_budget: a per-host, per-address pace the fleet learns, and a task that can wait

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-09-06

apply.workable.com refused 143 of 172 boards the hour a bundle first pulled,
on one per-address limit, and a fixed pace per process could not see that
two workers share an address. The budget row is keyed by upstream host and
egress address: a worker takes the host's next slot for its address before a
pull, a 429 doubles the gap and pushes the slot out, a success narrows it
toward the configured floor. tasks.not_before lets a worker put a pull back
until the slot opens without burning an attempt; worker_status.egress_group
says which address a worker speaks from. Additive.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "host_budget",
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("egress_group", sa.Text(), nullable=False),
        sa.Column("pace_seconds", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_allowed_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ok", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("refused", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("host", "egress_group"),
    )
    op.add_column(
        "tasks", sa.Column("not_before", postgresql.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column("worker_status", sa.Column("egress_group", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("worker_status", "egress_group")
    op.drop_column("tasks", "not_before")
    op.drop_table("host_budget")
