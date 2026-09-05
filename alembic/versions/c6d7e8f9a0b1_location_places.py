"""locations.places: every place a string names, not only one

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-09-05

The first classification allowed one place per string, so "London,
Montreal, Singapore" and "United States and Canada" were unplaceable and a
posting listed in New York and London dropped out of a United States
filter. 1,365 of 8,754 strings on production are shaped like that. One
array per string; the existing columns keep the first entry for display.
Additive; the backfill is one statement over 8,754 rows.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "locations",
        sa.Column(
            "places",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE locations
        SET places = jsonb_build_array(
            jsonb_build_object('country', country, 'region', region, 'city', city))
        WHERE country IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("locations", "places")
