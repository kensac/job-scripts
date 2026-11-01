"""worker_status: when each worker's telemetry export last succeeded and failed

The telemetry layer never raises, so a failing OTLP exporter only counted
itself in jobtracker_telemetry_exports_total, and no alert rule read that
counter. Each heartbeat now reports the two times, and
telemetry_export_failing (api.health.fleet) reads them. Additive.

Revision ID: 135806ab8657
Revises: 7e5c9370784e
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "135806ab8657"
down_revision: str | None = "7e5c9370784e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_status",
        sa.Column("telemetry_ok_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "worker_status",
        sa.Column("telemetry_failed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("worker_status", "telemetry_failed_at")
    op.drop_column("worker_status", "telemetry_ok_at")
