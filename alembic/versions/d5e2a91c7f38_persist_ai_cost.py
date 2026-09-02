"""ai_queries.cost_usd, api_usage.cost_usd + cached_tokens

Revision ID: d5e2a91c7f38
Revises: c4d9e17b3f60
Create Date: 2026-09-02 10:00:00.000000

Cost was computed five times over and stored nowhere: every copy fed a
Prometheus counter, which resets on restart and is sampled, not summed. So the
only durable spend figure in the system was ai_batches.est_cost_usd, which
covers batched work only - 23% of calls. Nothing could answer "what did last
week cost".

Cost is written at call time rather than derived at read time because the price
table changes (Sonnet 5's intro price already has). Deriving on read means
editing a dict silently rewrites history; storing it freezes the price that was
actually charged.

The backfill prices existing rows at TODAY's rates, which is an estimate for
anything billed at a since-changed rate. That is a one-time approximation for
rows that have no better answer, not the ongoing mechanism.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd5e2a91c7f38'
down_revision: Union[str, None] = 'c4d9e17b3f60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_ai_queries() -> None:
    """Price historical rows from the one price table, rather than restating it
    in SQL - a second copy here is exactly the drift this migration exists to
    end."""
    from core.pricing import PRICES_PER_MTOK, cost_sql

    expr = cost_sql(
        model_rate_in="CAST(:rate_in AS numeric)",
        model_rate_out="CAST(:rate_out AS numeric)",
        batched="batch_id IS NOT NULL",
    )
    for model, (rate_in, rate_out) in PRICES_PER_MTOK.items():
        op.execute(
            sa.text(
                f"UPDATE ai_queries SET cost_usd = {expr} "
                "WHERE model = :model AND cost_usd IS NULL"
            ).bindparams(model=model, rate_in=str(rate_in), rate_out=str(rate_out))
        )


def _ai_queries_exists() -> bool:
    """ai_queries is in alembic's _FOREIGN_TABLES: core/store.py owns its
    CREATE and runs AFTER alembic on a virgin database (db.init_schema
    migrates first, then core.store is imported). So this revision cannot
    assume the table is there - on a fresh database it is not, and the column
    arrives from store.py's CREATE TABLE body instead.

    Those two definitions must change together. store.py's own
    ALTER..ADD COLUMN IF NOT EXISTS lines cannot serve an existing database,
    because init_schema returns early whenever the table is already present.
    """
    bind = op.get_bind()
    return bind.execute(sa.text("SELECT to_regclass('public.ai_queries')")).scalar() is not None


def upgrade() -> None:
    op.add_column('api_usage', sa.Column('cost_usd', sa.Numeric(12, 6), nullable=True))
    op.add_column(
        'api_usage',
        sa.Column('cached_tokens', sa.BigInteger(), nullable=False, server_default='0'),
    )
    if not _ai_queries_exists():
        return
    op.add_column('ai_queries', sa.Column('cost_usd', sa.Numeric(12, 6), nullable=True))
    # Spend is always read as a time series over a window; without this the
    # dashboard seq-scans 74k rows for every panel.
    op.create_index(
        'idx_ai_queries_cost_created', 'ai_queries', ['created_at'],
        postgresql_where=sa.text('cost_usd IS NOT NULL'),
    )
    _backfill_ai_queries()


def downgrade() -> None:
    if _ai_queries_exists():
        op.drop_index('idx_ai_queries_cost_created', table_name='ai_queries')
        op.drop_column('ai_queries', 'cost_usd')
    op.drop_column('api_usage', 'cached_tokens')
    op.drop_column('api_usage', 'cost_usd')
