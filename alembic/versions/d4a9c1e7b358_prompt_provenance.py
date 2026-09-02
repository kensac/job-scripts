"""ai_prompts, ai_prompt_samples, and ai_batches.prompt_id

Revision ID: d4a9c1e7b358
Revises: c3f7a1b8e942
Create Date: 2026-09-02 09:40:00.000000

Batched extraction records what it spent and nothing about what it asked.
71,725 requests of comp, requirements and mail classification carry tokens,
cost and a model, so "what changed when this prompt changed" - the question
ai_queries answers for filters - cannot be asked of any of them.

ai_prompts stores each distinct instruction text ONCE. Measured on production,
68,735 ai_queries rows carry 21 distinct instruction texts between them, which
is 75 MB stored per row against about 32 KB stored per distinct text. That
2,300x redundancy is why this table can afford to keep the text rather than
only a hash: a hash would answer "did it change" and cost the same, and would
not answer "what was it".

ai_prompt_samples keeps a bounded sample of outputs per prompt version rather
than all of them. A sample is what answers "what changed", and it is what
every audit on this codebase has actually used - the gpt-5-nano fabrication
finding came from 60 postings, the embedding recall curve from 400. Storing
all 71,725 outputs would be roughly 29 MB per pass to enable a comparison
nobody runs at census scale, while the destination tables already hold the
current answer. What they do not hold is the PREVIOUS answer, which is exactly
what a sample preserves.

NOTHING HERE IS A RESOLUTION KEY. ai_queries keys custom verdicts on
(url, check_type, prompt_hash) so that changing a filter's prompt makes prior
verdicts unreachable - correct for filters, and catastrophic if generalised:
a comp or requirements prompt change would invalidate 49k extracted rows and
re-pay for the catalog. These tables are read by people, not by the pipeline.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd4a9c1e7b358'
down_revision: Union[str, None] = 'c3f7a1b8e942'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_prompts',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        # sha256 of the exact bytes sent. Unique because the whole point is one
        # row per distinct text, however many sweeps send it.
        sa.Column('prompt_hash', sa.Text(), nullable=False, unique=True),
        sa.Column('purpose', sa.Text(), nullable=False),
        sa.Column('instructions', sa.Text(), nullable=False),
        sa.Column('first_seen_at', postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        # Moves on every sweep, so a prompt that stopped being used is visible
        # as one whose last_seen_at is old rather than by absence.
        sa.Column('last_seen_at', postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('batches', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
    )
    op.create_index('idx_ai_prompts_purpose', 'ai_prompts', ['purpose', 'last_seen_at'])

    op.create_table(
        'ai_prompt_samples',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column('prompt_id', sa.BigInteger(),
                  sa.ForeignKey('ai_prompts.id', ondelete='CASCADE'), nullable=False),
        # Whatever the caller keyed its specs by - a url for the catalog
        # sweeps, a message id for mail. Not a foreign key: the sample outlives
        # the row it describes, which is most of its value when a posting is
        # gone and the question is what we used to say about it.
        sa.Column('custom_id', sa.Text(), nullable=False),
        sa.Column('output', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    # The cap is enforced per prompt by counting, so this index serves both the
    # count and the read.
    op.create_index('idx_ai_prompt_samples_prompt', 'ai_prompt_samples', ['prompt_id', 'id'])

    op.add_column(
        'ai_batches',
        sa.Column('prompt_id', sa.BigInteger(), sa.ForeignKey('ai_prompts.id'), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('ai_batches', 'prompt_id')
    op.drop_index('idx_ai_prompt_samples_prompt', table_name='ai_prompt_samples')
    op.drop_table('ai_prompt_samples')
    op.drop_index('idx_ai_prompts_purpose', table_name='ai_prompts')
    op.drop_table('ai_prompts')
