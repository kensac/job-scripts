"""sources: how often a board is pulled

Revision ID: d5e0f1a2b3c4
Revises: c4d9e1f0a2b3
Create Date: 2026-09-04

ingest_interval_hours: hours between pulls of one board. 1 is the hourly
cycle every source had before. A few hundred company boards that post a new
entry-level role a few times a month do not need 24 pulls a day; a whole
bundle of them is set to 24 in one write through the category switch.
"""

import sqlalchemy as sa
from alembic import op

revision = "d5e0f1a2b3c4"
down_revision = "c4d9e1f0a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "ingest_interval_hours", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("sources", "ingest_interval_hours")
