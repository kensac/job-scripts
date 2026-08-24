"""task worker attribution

Revision ID: a3d5e8f01c22
Revises: b1cf341e5fcc
Create Date: 2026-08-24 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a3d5e8f01c22'
down_revision: Union[str, None] = 'b1cf341e5fcc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('worker', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('tasks', 'worker')
