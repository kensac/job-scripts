"""jobs: comp_period, comp_currency, comp_basis

Revision ID: c4d9e17b3f60
Revises: b3f8d21c5a04
Create Date: 2026-09-01 15:20:00.000000

The comp column stored a yearly number with nothing beside it saying how that
number was derived, so a wrong one was indistinguishable from a right one.
_annualize also did not know "weekly", which put both $5,000/yr (a weekly wage
stored raw) and $4,160,000/yr (a weekly wage multiplied by 2080) into the same
sortable column.

Recording the period, currency and basis makes the annual figure auditable and
re-derivable without another AI pass.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c4d9e17b3f60'
down_revision: Union[str, None] = 'b3f8d21c5a04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('comp_period', sa.Text(), nullable=True))
    op.add_column('jobs', sa.Column('comp_currency', sa.Text(), nullable=True))
    op.add_column('jobs', sa.Column('comp_basis', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'comp_basis')
    op.drop_column('jobs', 'comp_currency')
    op.drop_column('jobs', 'comp_period')
