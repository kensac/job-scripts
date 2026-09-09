"""merge saved view and filter constraints

Revision ID: 7ac07584bcb5
Revises: 3ffc161e4389, 5a736c33a9cf
Create Date: 2026-09-09 03:53:20.283063

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7ac07584bcb5'
down_revision: Union[str, None] = ('3ffc161e4389', '5a736c33a9cf')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
