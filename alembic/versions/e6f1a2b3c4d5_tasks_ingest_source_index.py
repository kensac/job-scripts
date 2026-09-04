"""tasks: index on the ingest task's source

Revision ID: e6f1a2b3c4d5
Revises: d5e0f1a2b3c4
Create Date: 2026-09-04

Every worker asks once a minute, per active source, whether that source has
a pending ingest and when its last one ran (the per-source interval, #320).
Each of those was a sequential scan of tasks: on 2026-09-04, 738 scans and
1.4 seconds per tick per worker, six workers. The admin queue filter and the
ingest summary read the same expression. Partial on kind so it stays the
size of the ingest history rather than the whole table.
"""

import sqlalchemy as sa
from alembic import op

revision = "e6f1a2b3c4d5"
down_revision = "d5e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_tasks_ingest_source",
        "tasks",
        [sa.text("(payload->>'source')"), sa.text("created_at DESC")],
        postgresql_where=sa.text("kind = 'ingest_source'"),
    )


def downgrade() -> None:
    op.drop_index("idx_tasks_ingest_source", table_name="tasks")
