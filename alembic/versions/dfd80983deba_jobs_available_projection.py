"""jobs.available: catalog.AVAILABLE, stored

Readers that compute availability per row with catalog.AVAILABLE paid for it
on every read: on production (2026-10-10) the AI-eligible count went from
2.0 s to 6.5 s, and one board recompute from 27 s to 38 s, at 992
recomputes a day. A stored column costs readers nothing, and is written
only where a source's answer changes (a few hundred observations an hour in
steady state) plus an hourly reconcile for switches and the pattern setting
(about 10 s to evaluate over the catalog). NULL is cannot tell.

Nullable with no default, so adding it rewrites nothing. The reconcile
fills it in id chunks, which is a bulk update of up to every row (about 1M
rows, 422 MB of heap), so autovacuum is set first: at the stock scale
factor of 0.2 the table holds about 200k dead versions before vacuum frees
any space; at 0.02 about 20k, the values 7c0b33a7fd95 and 40e76006134a
chose. ALTER TABLE SET takes SHARE UPDATE EXCLUSIVE and rewrites nothing.

Revision ID: dfd80983deba
Revises: 677b36b26ff5
Create Date: 2026-10-10 15:29:08.671750

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dfd80983deba"
down_revision: str | None = "677b36b26ff5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTOVACUUM = {
    "autovacuum_vacuum_scale_factor": "0.02",
    "autovacuum_analyze_scale_factor": "0.01",
}


def upgrade() -> None:
    # Both statements take a brief lock on a table every pull writes; a
    # minute is bd1e66f153c3's judgment of what a start can afford.
    op.execute("SET LOCAL lock_timeout = '60s'")
    op.add_column("jobs", sa.Column("available", sa.Boolean(), nullable=True))
    op.execute(
        "ALTER TABLE jobs SET (" + ", ".join(f"{k} = {v}" for k, v in _AUTOVACUUM.items()) + ")"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE jobs RESET ({', '.join(_AUTOVACUUM)})")
    op.drop_column("jobs", "available")
