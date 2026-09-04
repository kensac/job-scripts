"""listings: everything a board returned, kept or screened, with its text

Revision ID: a8b9c0d1e2f3
Revises: f7a2b3c4d5e6
Create Date: 2026-09-04

screened_postings kept only what the title pattern dropped. The rule now is
that every listing a board returns is stored, with the posting text the
listing call already carried (Greenhouse with content=true, Lever, Ashby)
and the raw record minus that text, so a backtest of a pattern or a backfill
of content never has to re-fetch a board or scrape a page: scraping is the
action that gets the fleet blocked, and the boards hand this over for free
in the same call. Same table, renamed, plus a kept flag and the two columns.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a8b9c0d1e2f3"
down_revision = "f7a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("screened_postings", "listings")
    op.execute("ALTER INDEX idx_screened_postings_source RENAME TO idx_listings_source")
    op.add_column(
        "listings",
        sa.Column("kept", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "listings",
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.add_column(
        "listings",
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("listings", "raw")
    op.drop_column("listings", "description")
    op.drop_column("listings", "kept")
    op.execute("ALTER INDEX idx_listings_source RENAME TO idx_screened_postings_source")
    op.rename_table("listings", "screened_postings")
