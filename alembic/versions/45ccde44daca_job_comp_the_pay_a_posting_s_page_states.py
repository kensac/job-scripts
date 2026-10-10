"""job_comp: the pay a posting's page states

Pay moves off `jobs` into the derived table it is, beside job_requirements and
job_embeddings, so `jobs` keeps only what listings say. Additive: the comp
sweep writes both while readers still read `jobs`, and the copy_job_comp task
fills this table from `jobs` (110,243 extracted postings on 2026-10-10).

Revision ID: 45ccde44daca
Revises: e40aea5f9b10
Create Date: 2026-10-10 14:22:49.614919

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '45ccde44daca'
down_revision: Union[str, None] = 'e40aea5f9b10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('job_comp',
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('comp_min', sa.BigInteger(), nullable=True),
    sa.Column('comp_max', sa.BigInteger(), nullable=True),
    sa.Column('comp_text', sa.Text(), nullable=True),
    sa.Column('comp_period', sa.Text(), nullable=True),
    sa.Column('comp_currency', sa.Text(), nullable=True),
    sa.Column('comp_basis', sa.Text(), nullable=True),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('content_hash', sa.Text(), nullable=True),
    sa.Column('content_row_id', sa.BigInteger(), nullable=True),
    sa.Column('extracted_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('url')
    )


def downgrade() -> None:
    op.drop_table('job_comp')
