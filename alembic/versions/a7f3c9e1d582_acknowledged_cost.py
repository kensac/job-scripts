"""task_model_overrides.acknowledged_cost

Revision ID: a7f3c9e1d582
Revises: f1a2b3c4d5e6
Create Date: 2026-09-02 16:55:00.000000

A model change that costs more than ten times the one it replaces is refused
unless it is acknowledged. Recorded on the row, at decision time, because "he
was told and went ahead" is a different fact from "he changed it", and the
monthly review is exactly where that distinction is wanted.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7f3c9e1d582'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'task_model_overrides',
        sa.Column('acknowledged_cost', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
    )


def downgrade() -> None:
    op.drop_column('task_model_overrides', 'acknowledged_cost')
