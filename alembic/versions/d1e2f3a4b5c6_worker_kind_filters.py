"""worker_status records what each worker will claim

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-09-04

queue_stalled asks whether an idle worker is sitting next to work it should
have taken. It compared every idle worker against every pending task, which
was correct while no worker filtered kinds. JOBTRACKER_WORKER_EXCLUDE_KINDS
made that assumption false: a host that legitimately refuses ingest reads as
stalled whenever the queue is mostly ingest, which on this fleet it usually
is.

The filters live in host environment, so the database cannot infer them. The
worker reports them on the heartbeat it already writes.

Additive, defaulted empty. A worker on an older image writes neither column
and keeps the previous behaviour, so no lockstep is needed for the roll.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "worker_status",
        sa.Column(
            "kinds",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "worker_status",
        sa.Column(
            "excluded_kinds",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("worker_status", "excluded_kinds")
    op.drop_column("worker_status", "kinds")
