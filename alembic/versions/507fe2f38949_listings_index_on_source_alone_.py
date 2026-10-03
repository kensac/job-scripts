"""listings index on source alone, fillfactor 80

A pull rewrites a listings row only when it changed or its last_seen_at has
aged past listings_seen_refresh_hours. Those remaining updates can be
heap-only (HOT: no new index entries) only when no indexed column changes and
the page has room for the new version. idx_listings_source carried
last_seen_at, which every refresh moves, so no refresh could be HOT: 860 of
1.17M updates a day were (pg_stat_user_tables, 2026-10-03). Every reader
(admin pattern preview and screened list) and the retention delete filter by
source alone.

Fillfactor 80, chosen by measurement on a 3,000-row copy of the table's
shape (one row in three with TOASTed text), six rounds of refreshing every
row: with the source-only index, 0 of 18,000 updates were HOT at fillfactor
100, 4,041 at 90 and 10,614 at 80. WAL per row in a round between
checkpoints was 847 bytes with the old index, 836 with the new one at 100,
and by the sixth round 591 at 90 and 289 at 80. The row shape was synthetic, so the
production HOT ratio is to be read off pg_stat_user_tables after the roll.

Fillfactor governs pages written from now on, not the pages already there.
It reaches existing rows as they are rewritten: a non-HOT update places the
new version as an insert would, under the new fillfactor. Nothing here
rewrites the table, which would lock it.

Safe on the live table (650k rows): both index builds are CONCURRENTLY, so
ingest keeps writing throughout, and the new index exists before the old one
is dropped, so the retention delete always has one. ALTER TABLE SET
(fillfactor) takes SHARE UPDATE EXCLUSIVE, which blocks neither reads nor
writes. A build that died midway leaves an INVALID index that IF NOT EXISTS
would mistake for a finished one, so an invalid leftover is dropped first.
A concurrent build waits for every transaction older than itself; the lock
timeout turns a wait that will not end into a failed start that retries, as
in bd1e66f153c3.

Revision ID: 507fe2f38949
Revises: bd1e66f153c3
Create Date: 2026-10-03 10:21:15.219333

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "507fe2f38949"
down_revision: str | None = "bd1e66f153c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _build(name: str, columns: str) -> None:
    invalid = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND NOT i.indisvalid"
        ),
        {"name": name},
    )
    if invalid.first():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON listings ({columns})")


def _swap(build: str, columns: str, drop: str) -> None:
    with op.get_context().autocommit_block():
        # bd1e66f153c3's judgment: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        _build(build, columns)
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {drop}")
        op.execute("RESET lock_timeout")


def upgrade() -> None:
    _swap("idx_listings_by_source", "source", "idx_listings_source")
    op.execute("ALTER TABLE listings SET (fillfactor = 80)")


def downgrade() -> None:
    op.execute("ALTER TABLE listings RESET (fillfactor)")
    _swap("idx_listings_source", "source, last_seen_at", "idx_listings_by_source")
