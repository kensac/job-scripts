"""data health alerts

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-30 15:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'health_alerts',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('severity', sa.Text(), nullable=False, server_default=sa.text("'warning'")),
        sa.Column('message', sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column('detail', JSONB(), nullable=True),
        sa.Column('first_seen', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('notified_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # One open alert per (kind, subject): re-detections refresh it instead of
    # piling up, which is what keeps the mail from becoming noise.
    op.create_index(
        'uq_health_alerts_open', 'health_alerts', ['kind', 'subject'],
        unique=True, postgresql_where=sa.text('resolved_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_health_alerts_open', 'health_alerts')
    op.drop_table('health_alerts')
