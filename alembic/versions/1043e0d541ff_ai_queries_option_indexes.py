"""ai_queries: (config_name, created_at) and (worker, created_at)

GET /admin/queries/options lists the config names and workers seen in the
last 30 days. As SELECT DISTINCT it read every row of ai_queries: 1.1 s and
1.26 s warm on production (2.07M rows, 2026-10-04), up to 7.3 s under load.
The route now skip-scans the distinct values and checks each for a recent
row; both steps need an index that leads with the column, and the second is
an equality plus a range on created_at, so each index is (column,
created_at). check_type and status already have one each.

Safe on the live table: CONCURRENTLY, so writers to ai_queries keep going
for the whole build. A concurrent build waits for every transaction older
than itself, so the lock timeout turns a wait that will not end into a
failed start that retries (bd1e66f153c3). An interrupted build leaves an
INVALID index that IF NOT EXISTS would accept as finished, so one is dropped
first (507fe2f38949).

Build time on a 2.07M-row copy shaped by production_profile.json (1.6 GB
heap, 128 MB shared_buffers, idle laptop, 2026-10-03): 3.4 s and 2.5 s, 75 MB
and 62 MB. A seq scan of that copy took 0.3 s against production's 4 s warm
and 11.8 s loaded, and a concurrent build reads the heap twice, so expect
tens of seconds to a couple of minutes per index on production: an inference
from those ratios, not a measurement. Writers are not blocked meanwhile, but
the api and workers start behind the schema lock until it finishes.

Revision ID: 1043e0d541ff
Revises: c1746d710146
Create Date: 2026-10-03 22:11:06.121273

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1043e0d541ff"
down_revision: str | None = "c1746d710146"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = {
    "idx_ai_queries_config_recent": "config_name",
    "idx_ai_queries_worker_recent": "worker",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # bd1e66f153c3's judgment: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        for name, column in _INDEXES.items():
            invalid = op.get_bind().execute(
                sa.text(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": name},
            )
            if invalid.first():
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.create_index(
                name,
                "ai_queries",
                [column, "created_at"],
                postgresql_concurrently=True,
                if_not_exists=True,
            )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        for name in _INDEXES:
            op.drop_index(
                name, table_name="ai_queries", postgresql_concurrently=True, if_exists=True
            )
        op.execute("RESET lock_timeout")
