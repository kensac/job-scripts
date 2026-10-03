"""board_visible_recomputes: when each person's board was last recomputed

Revision ID: 221f5df878c9
Revises: 7c0b33a7fd95
Create Date: 2026-10-03

The recompute replaced a person's whole board_visible set every cycle: 726
recomputes rewrote 1.28M rows and 3.97 GB of WAL in 36 hours of production
(pg_stat_statements, 2026-10-03). It now writes only the rows that changed, so
a row's computed_at stops meaning "when the board was last computed". This
table carries that time instead, one row per person.

Additive and empty at first. A worker still on the old code keeps deleting and
reinserting with now() and never touches this table; the readers take the
later of this row and the board's rows, so either writer's last recompute is
what they report. See api.board.visibility.computed_at.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "221f5df878c9"
down_revision: str | None = "7c0b33a7fd95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "board_visible_recomputes",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("computed_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("board_visible_recomputes")
