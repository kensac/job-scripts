"""locations: every distinct location string a board wrote, classified once

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-09-05

Location exclusion matched words in the raw string, so "UK" missed "London"
and "Canada" missed "Toronto, ON". One row per distinct string (8,735 on
2026-09-04 against 71,574 values), classified by a model once and read by
the visibility predicate ever after. Additive.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("remote", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "classified_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("text"),
    )


def downgrade() -> None:
    op.drop_table("locations")
