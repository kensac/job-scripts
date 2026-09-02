"""api_usage carries fleet work, which belongs to no user

Revision ID: e8b2c4d9f731
Revises: c3f7a1b8e942
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e8b2c4d9f731"
down_revision = "c3f7a1b8e942"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Catalog-wide work - comp, requirements, verification - is charged to
    # nobody in particular. NULL says so, rather than attributing the whole
    # fleet's spend to whichever admin happens to be user 1.
    op.alter_column("api_usage", "user_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("api_usage", sa.Column("batched", sa.Boolean(), server_default=sa.false()))
    op.create_index("idx_api_usage_purpose", "api_usage", ["purpose", "created_at"])

    # Everything already spent on batched work. Without this the cost centre is
    # correct from today and silent about the largest bill the system has run
    # up - mail classification alone is $18.49 that never wrote a verdict row
    # and so never reached the spend page.
    #
    # Guarded on the table existing because ai_batches is created by
    # core/store.py, which runs AFTER alembic on a virgin database.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT to_regclass('public.ai_batches')")).scalar() is not None:
        bind.execute(
            sa.text(
                """
                INSERT INTO api_usage (user_id, created_at, key_source, purpose, model,
                                       prompt_tokens, completion_tokens, total_tokens,
                                       cached_tokens, batched, cost_usd)
                SELECT NULL, b.submitted_at, 'server', b.purpose, b.model,
                       COALESCE(b.input_tokens, 0), COALESCE(b.output_tokens, 0),
                       COALESCE(b.input_tokens, 0) + COALESCE(b.output_tokens, 0),
                       0, TRUE, b.est_cost_usd
                FROM ai_batches b
                WHERE COALESCE(b.input_tokens, 0) + COALESCE(b.output_tokens, 0) > 0
                """
            )
        )


def downgrade() -> None:
    op.drop_index("idx_api_usage_purpose", table_name="api_usage")
    op.drop_column("api_usage", "batched")
    op.alter_column("api_usage", "user_id", existing_type=sa.BigInteger(), nullable=False)
