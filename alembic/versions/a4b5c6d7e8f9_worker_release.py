"""worker_status.release: the image commit each worker runs

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-09-05

On 2026-09-04 gcp-vps ran two rolls behind the rest of the fleet for an
hour, pre-migration code against a migrated database, and nothing alerted:
its deploy workflow had never had a runner, so its jobs sat queued rather
than failing. Each heartbeat now reports the release the worker was built
from, and the api compares it with its own. Additive.
"""

import sqlalchemy as sa
from alembic import op

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_status", sa.Column("release", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("worker_status", "release")
