"""drop listings.pattern; every listing points at its stored pattern

The contract half of 40e76006134a. listings.pattern was a copy of the
source's title pattern on every row; title_patterns holds each text once and
listings.pattern_id points at it. Readers moved to the pointer one release
before the copy stopped being written, and drop_listing_pattern_copies
emptied what no pull rewrote. Every image that names listings.pattern must be
gone from the fleet before this applies: dropping a column breaks any
statement that names it.

Nothing here can drop data. ck_listings_pattern_moved asserts that every row
has no copy and has a pointer. It is added NOT VALID (no scan) and then
validated, which scans the table under SHARE UPDATE EXCLUSIVE: reads and
writes continue. If any row still holds a copy or lacks a pointer, VALIDATE
raises, nothing is dropped, and the container does not start. fk_listings_pattern
is validated the same way. Then one short transaction sets pattern_id NOT
NULL, which the validated guard proves without a scan, drops the guard and
drops the column. That holds ACCESS EXCLUSIVE briefly and rewrites nothing.

Dropping the column frees no heap space by itself. The 492 MB the copy held
(2026-10-10) is returned by VACUUM FULL listings, which is run by hand.

Revision ID: 8aa6dcf6aede
Revises: 3c0273da3222
Create Date: 2026-10-10 01:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8aa6dcf6aede"
down_revision: str | None = "3c0273da3222"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD = "ck_listings_pattern_moved"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # bd1e66f153c3's judgment: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        op.execute(f"ALTER TABLE listings DROP CONSTRAINT IF EXISTS {_GUARD}")
        op.execute(
            f"ALTER TABLE listings ADD CONSTRAINT {_GUARD} "
            "CHECK (pattern IS NULL AND pattern_id IS NOT NULL) NOT VALID"
        )
        op.execute(f"ALTER TABLE listings VALIDATE CONSTRAINT {_GUARD}")
        op.execute("ALTER TABLE listings VALIDATE CONSTRAINT fk_listings_pattern")
        op.execute("RESET lock_timeout")
    op.execute("SET LOCAL lock_timeout = '60s'")
    op.alter_column("listings", "pattern_id", existing_type=sa.BIGINT(), nullable=False)
    op.execute(f"ALTER TABLE listings DROP CONSTRAINT {_GUARD}")
    op.drop_column("listings", "pattern")


def downgrade() -> None:
    op.add_column("listings", sa.Column("pattern", sa.TEXT(), nullable=True))
    op.alter_column("listings", "pattern_id", existing_type=sa.BIGINT(), nullable=True)
