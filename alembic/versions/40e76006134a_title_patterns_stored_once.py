"""title patterns stored once; listings point at them

Every listings row carried a copy of its source's title pattern: 1,060,806
rows, 4 distinct values, 492 MB of the table's 863 MB of row data, and every
row equal to its source's current pattern (production, 2026-10-10). Each
distinct pattern now has one row in title_patterns, keyed by the SHA-256 of
its text, and listings.pattern_id points at it. This is the expand step:
pattern is still written, and becomes nullable so that a later release can
stop writing it, empty it and drop it (migrations.md).

The upsert's change check compares pattern_id, so a row with none is a
change: every row a pull still lists is rewritten by that pull and gains its
id. That is about the volume the daily refresh already rewrites
(listings_seen_refresh_hours, 24 by default; 1.17M updates a day on
2026-10-03), brought forward into one cycle of pulls. Autovacuum is set for
it first: at the stock scale factor of 0.2 the table holds about 210k dead
versions before vacuum frees any space; at 0.02 about 21k, the values
7c0b33a7fd95 chose for review_gate_decisions. ALTER TABLE SET takes SHARE
UPDATE EXCLUSIVE and rewrites nothing.

The foreign key is NOT VALID: adding it validated would scan the whole table
under SHARE ROW EXCLUSIVE, which blocks every pull's writes for the scan.
Unvalidated, it still checks every row written from here on, and every
existing row is NULL. The release that drops pattern validates it.

Revision ID: 40e76006134a
Revises: c4dd7d081b39
Create Date: 2026-10-10 00:52:13.559090

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "40e76006134a"
down_revision: str | None = "c4dd7d081b39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTOVACUUM = {
    "autovacuum_vacuum_scale_factor": "0.02",
    "autovacuum_analyze_scale_factor": "0.01",
}


def upgrade() -> None:
    op.create_table(
        "title_patterns",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("digest", sa.LargeBinary(), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("digest"),
    )
    # Every statement below takes a brief lock on a table each pull writes;
    # a minute is bd1e66f153c3's judgment of what a start can afford.
    op.execute("SET LOCAL lock_timeout = '60s'")
    op.add_column("listings", sa.Column("pattern_id", sa.BigInteger(), nullable=True))
    op.alter_column("listings", "pattern", existing_type=sa.TEXT(), nullable=True)
    op.execute(
        "ALTER TABLE listings ADD CONSTRAINT fk_listings_pattern "
        "FOREIGN KEY (pattern_id) REFERENCES title_patterns (id) NOT VALID"
    )
    op.execute(
        "ALTER TABLE listings SET ("
        + ", ".join(f"{k} = {v}" for k, v in _AUTOVACUUM.items())
        + ")"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE listings RESET ({', '.join(_AUTOVACUUM)})")
    op.drop_constraint("fk_listings_pattern", "listings", type_="foreignkey")
    op.drop_column("listings", "pattern_id")
    op.alter_column("listings", "pattern", existing_type=sa.TEXT(), nullable=False)
    op.drop_table("title_patterns")
