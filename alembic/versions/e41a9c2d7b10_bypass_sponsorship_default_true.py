"""bypass sponsorship default true

Revision ID: e41a9c2d7b10
Revises: 09f73ae6af6b
Create Date: 2026-08-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e41a9c2d7b10'
down_revision: Union[str, None] = '09f73ae6af6b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows keep their chosen value; only new users get the new default.
    op.alter_column(
        'user_settings',
        'bypass_sponsorship_filter',
        server_default=sa.text('true'),
    )


def downgrade() -> None:
    op.alter_column(
        'user_settings',
        'bypass_sponsorship_filter',
        server_default=sa.text('false'),
    )
