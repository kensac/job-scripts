"""screened_postings: what a board listed that its title pattern did not admit

Revision ID: f7a2b3c4d5e6
Revises: e6f1a2b3c4d5
Create Date: 2026-09-04

A title pattern kept 6,546 of 69,912 postings the company boards fetched in
six hours on 2026-09-04 and the other 63,366 were dropped without a record,
so nobody could say whether the pattern was excluding entry-level roles that
do not use the words it looks for (Databricks: 6 of 866 admitted). This
table keeps the excluded postings' titles, refreshed on every pull, so a
candidate pattern can be evaluated against a month of what the boards
actually listed before it replaces the live one. Separate from jobs on
purpose: nothing downstream (visibility, AI checks, boards) has to learn a
new state, and a posting that a new pattern admits simply arrives in jobs on
the next pull.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f7a2b3c4d5e6"
down_revision = "e6f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("idx_screened_postings_source", table_name="screened_postings")
    op.drop_table("screened_postings")
