"""extension recipes

Revision ID: 7e2f9c4b1a63
Revises: b48a878a0be1
Create Date: 2026-09-09 21:50:00.000000

The recipe table a config-driven reader runs from, published from the API so
a broken site can be fixed without a browser release; the copy bundled in the
extension is the fallback. Additive: a new table, no data step, no preflight.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7e2f9c4b1a63"
down_revision: str | None = "b48a878a0be1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extension_recipes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published_by", sa.Text(), nullable=True),
        sa.Column(
            "published_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("adapter", "revision", name="uq_extension_recipes_adapter_revision"),
    )
    op.create_index(
        "idx_extension_recipes_adapter_enabled", "extension_recipes", ["adapter", "enabled"]
    )


def downgrade() -> None:
    op.drop_index("idx_extension_recipes_adapter_enabled", table_name="extension_recipes")
    op.drop_table("extension_recipes")
