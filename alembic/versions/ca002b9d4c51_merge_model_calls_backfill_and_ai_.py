"""merge model_calls backfill and ai_prompts drop heads

Revision ID: ca002b9d4c51
Revises: 53da75ac7394, 72dc0fd47e51
Create Date: 2026-10-10 01:38:44.640728

#882 and #886 each added a migration on 5a7c2e9d1b40, so main held two
heads and upgrade head refused to run. They touch different objects
(ai_prompts and its siblings; model_calls), so a merge revision is safe.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ca002b9d4c51'
down_revision: Union[str, None] = ('53da75ac7394', '72dc0fd47e51')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
