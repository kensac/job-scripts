"""ai_batch_errors: every per-request error a provider batch returned

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-04

On 2026-09-04, 49 requirements batches on gpt-5-nano failed every one of
their 21,525 requests. The provider said why, per request, in the error
file; collection read it, set it on the result, and every handler that
skips errored results dropped it. Nothing stored it, so the alert could say
"rejected" and not one word more. Kanishk's rule: store everything the
provider returns and groom later; space is not the constraint. One row per
failed request, keyed by the provider batch id; additive.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_batch_errors",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("provider_batch_id", sa.Text(), nullable=False),
        sa.Column("custom_id", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_ai_batch_errors_batch", "ai_batch_errors", ["provider_batch_id"])


def downgrade() -> None:
    op.drop_index("idx_ai_batch_errors_batch", table_name="ai_batch_errors")
    op.drop_table("ai_batch_errors")
