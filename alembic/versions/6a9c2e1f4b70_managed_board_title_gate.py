"""Add managed board title gate configuration.

Revision ID: 6a9c2e1f4b70
Revises: 5e8a1c2d7f40
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "6a9c2e1f4b70"
down_revision = "5e8a1c2d7f40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "managed_boards",
        sa.Column("title_gate", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("managed_boards", "title_gate")
