"""board_visible: a person's board membership, computed by a task

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-09-05

The board's visibility predicate ran on every read, 2 to 7 seconds a sort
and twice with the total. Membership changes when a preference changes or a
verdict lands, so a worker computes it and the read is a lookup. Additive.
Empty at first; the api asks for every person's recompute at startup and
the scheduler every board_refresh_minutes.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "board_visible",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "computed_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "job_id"),
    )
    op.create_index("idx_board_visible_user", "board_visible", ["user_id", "computed_at"])


def downgrade() -> None:
    op.drop_index("idx_board_visible_user", table_name="board_visible")
    op.drop_table("board_visible")
