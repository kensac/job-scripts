"""ai batch registry + worker status

Revision ID: d9e0f1a2b3c4
Revises: c8f1a2b3d4e5
Create Date: 2026-08-25 02:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd9e0f1a2b3c4'
down_revision: Union[str, None] = 'c8f1a2b3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_batches',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('provider_batch_id', sa.Text(), nullable=False, unique=True),
        sa.Column('task_id', sa.BigInteger(), nullable=True),
        sa.Column('purpose', sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('requests', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
        sa.Column('completed', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
        sa.Column('failed_count', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
        sa.Column('status', sa.Text(), nullable=False, server_default=sa.text("'submitted'")),
        sa.Column('submitted_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index('idx_ai_batches_status', 'ai_batches', ['status', 'id'])
    op.create_index('idx_ai_batches_task', 'ai_batches', ['task_id'])
    op.create_table(
        'worker_status',
        sa.Column('name', sa.Text(), primary_key=True),
        sa.Column('started_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('current_task_id', sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('worker_status')
    op.drop_index('idx_ai_batches_task', 'ai_batches')
    op.drop_index('idx_ai_batches_status', 'ai_batches')
    op.drop_table('ai_batches')
