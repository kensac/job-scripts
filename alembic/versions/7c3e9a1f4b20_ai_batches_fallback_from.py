"""ai_batches: record the model a fallback batch ran instead of

A batch submitted on a fallback model, because the routed model's provider
reported the account cannot pay, names the routed model here. NULL for every
existing row and every batch on its routed model. Nullable with no default,
so adding it rewrites nothing.

Revision ID: 7c3e9a1f4b20
Revises: 2a88ef406f24
Create Date: 2026-11-01 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c3e9a1f4b20"
down_revision: str | None = "2a88ef406f24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_batches", sa.Column("fallback_from", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_batches", "fallback_from")
