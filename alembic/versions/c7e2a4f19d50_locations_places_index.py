"""locations: a GIN index on places for criterion matching

api.board.criteria finds every location a person's or board's location
criterion matches once per statement, by JSON containment of the
criterion's place in each location's places. This index answers that
containment instead of reading every location: on a production copy
(2026-11-26) the "United States" set took 254 ms against 664 ms, and the UK
and Canada exclusion 2 ms against 1,285 ms.

Built CONCURRENTLY, as bd1e66f153c3 builds on a live table, so the build
never blocks the classifier's writes to locations; the same lock_timeout
turns a wait on a transaction that will not finish into a failed start that
retries. An interrupted build leaves an INVALID index that IF NOT EXISTS
would accept, so one is dropped first. Additive.

Revision ID: c7e2a4f19d50
Revises: b4d81e2f6a07
Create Date: 2026-11-26 06:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e2a4f19d50"
down_revision: str | None = "b4d81e2f6a07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INVALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = 'idx_locations_places' AND NOT i.indisvalid"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # bd1e66f153c3's bound: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        if op.get_bind().execute(_INVALID).first():
            op.execute("DROP INDEX CONCURRENTLY idx_locations_places")
        op.create_index(
            "idx_locations_places",
            "locations",
            ["places"],
            unique=False,
            postgresql_using="gin",
            postgresql_ops={"places": "jsonb_path_ops"},
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_locations_places",
            table_name="locations",
            postgresql_concurrently=True,
            if_exists=True,
        )
