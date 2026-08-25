"""status history, comp columns, email digest

Revision ID: c8f1a2b3d4e5
Revises: a3d5e8f01c22
Create Date: 2026-08-25 01:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c8f1a2b3d4e5'
down_revision: Union[str, None] = 'a3d5e8f01c22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_job_history',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('job_id', sa.BigInteger(), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('old_status', sa.Text(), nullable=True),
        sa.Column('new_status', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('idx_user_job_history_row', 'user_job_history', ['user_id', 'job_id', 'id'])
    op.add_column('jobs', sa.Column('comp_min', sa.BigInteger(), nullable=True))
    op.add_column('jobs', sa.Column('comp_max', sa.BigInteger(), nullable=True))
    op.add_column('jobs', sa.Column('comp_text', sa.Text(), nullable=True))
    op.add_column('jobs', sa.Column('comp_extracted', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('user_settings', sa.Column('email_digest', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('user_settings', sa.Column('digest_token', sa.Text(), nullable=True))
    op.add_column('user_settings', sa.Column('last_digest_at', sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_unique_constraint('uq_user_settings_digest_token', 'user_settings', ['digest_token'])


def downgrade() -> None:
    op.drop_constraint('uq_user_settings_digest_token', 'user_settings')
    op.drop_column('user_settings', 'last_digest_at')
    op.drop_column('user_settings', 'digest_token')
    op.drop_column('user_settings', 'email_digest')
    op.drop_column('jobs', 'comp_extracted')
    op.drop_column('jobs', 'comp_text')
    op.drop_column('jobs', 'comp_max')
    op.drop_column('jobs', 'comp_min')
    op.drop_index('idx_user_job_history_row', 'user_job_history')
    op.drop_table('user_job_history')
