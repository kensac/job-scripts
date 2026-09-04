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
in the same call.

Additive on purpose. A rename would make the old name vanish the instant the
first container migrated, while every worker still on the previous image
writes to it from the ingest hot path for the rest of the roll. So this
creates listings beside screened_postings and copies the rows; the old table
is dropped by a later migration once no image references it. Rows old
workers write to screened_postings during the roll are not carried over;
they are refreshed on the next pull.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a8b9c0d1e2f3"
down_revision = "f7a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "listings",
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "locations", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("date_posted", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("kept", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
    op.create_index("idx_listings_source", "listings", ["source", "last_seen_at"])
    op.execute(
        """
        INSERT INTO listings
            (url, source, company, title, locations, date_posted, pattern,
             first_seen_at, last_seen_at)
        SELECT url, source, company, title, locations, date_posted, pattern,
               first_seen_at, last_seen_at
        FROM screened_postings
        ON CONFLICT (url) DO NOTHING
        """
    )
    # The kept half comes from the catalog, so pattern-preview judges a
    # candidate against both sides from the first minute rather than against
    # screened titles alone until every board has pulled again (up to a day
    # on the daily interval, never for a source with no board to re-pull).
    op.execute(
        """
        INSERT INTO listings
            (url, source, company, title, locations, date_posted, pattern, kept,
             first_seen_at, last_seen_at)
        SELECT j.url, j.source, j.company, j.title, j.locations, j.date_posted,
               COALESCE(s.title_pattern, ''), true, j.created_at, now()
        FROM jobs j JOIN sources s ON s.name = j.source
        WHERE j.active
        ON CONFLICT (url) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("idx_listings_source", table_name="listings")
    op.drop_table("listings")
