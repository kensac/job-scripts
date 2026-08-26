"""batch token and cost columns

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
Create Date: 2026-08-26 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd9e0f1a2b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_batches', sa.Column('est_tokens', sa.BigInteger(), nullable=False, server_default=sa.text('0')))
    op.add_column('ai_batches', sa.Column('input_tokens', sa.BigInteger(), nullable=False, server_default=sa.text('0')))
    op.add_column('ai_batches', sa.Column('output_tokens', sa.BigInteger(), nullable=False, server_default=sa.text('0')))
    op.add_column('ai_batches', sa.Column('est_cost_usd', sa.Numeric(12, 6), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_batches', 'est_cost_usd')
    op.drop_column('ai_batches', 'output_tokens')
    op.drop_column('ai_batches', 'input_tokens')
    op.drop_column('ai_batches', 'est_tokens')
