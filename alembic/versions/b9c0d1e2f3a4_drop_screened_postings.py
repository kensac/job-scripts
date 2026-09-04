"""drop screened_postings: the contract half of a8b9c0d1e2f3

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-09-04

a8b9c0d1e2f3 created listings beside screened_postings and copied it, so
workers on the previous image could keep writing to the old table for the
rest of that roll. Every image since reads and writes listings only, so the
old table is now a table nothing writes and nothing reads. Dropped, with its
ORM model, before it can outlive the reason it stayed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("idx_screened_postings_source", table_name="screened_postings")
    op.drop_table("screened_postings")


def downgrade() -> None:
    op.create_table(
        "screened_postings",
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "locations", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("date_posted", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column(
            "first_seen_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("url"),
    )
    op.create_index("idx_screened_postings_source", "screened_postings", ["source", "last_seen_at"])
