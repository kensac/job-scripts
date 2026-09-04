"""sources: company and title_pattern for company boards

Revision ID: c4d9e1f0a2b3
Revises: 7f2b41c8ae03
Create Date: 2026-09-04

company: the employer a company board belongs to. Lever, Ashby and Workday
list a company's own openings and never say whose, and the catalog needs a
name to match mail against. NULL for an aggregator, whose rows name it.

title_pattern: a case-insensitive regex a posting's title must match to be
ingested from this source; NULL takes everything. A company board lists every
opening and verify_new checks every active posting with cached text, so this
is what keeps a 2,300-opening board from costing 2,300 pairs of checks.
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d9e1f0a2b3"
down_revision = "7f2b41c8ae03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("company", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("title_pattern", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "title_pattern")
    op.drop_column("sources", "company")
