"""application dismissal

Revision ID: c3f7a1b8e942
Revises: b7c8d9e0f1a2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3f7a1b8e942"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("dismissed_at", sa.DateTime(timezone=True)))
    op.add_column("applications", sa.Column("dismissed_reason", sa.Text()))


def downgrade() -> None:
    op.drop_column("applications", "dismissed_reason")
    op.drop_column("applications", "dismissed_at")
