"""drop the usage copies the call ledger replaced

The contract step of the call ledger (docs/agents/architecture-migration.md,
"The ledger of paid model calls"). model_calls holds every paid call, the old
ones copied in by the backfill (#886). Spend, the budget and the batch screens
read it (#892, #893). Nothing writes the copies since #923, and
tasks.usage_copies (#926) emptied them:

- api_usage, the old usage ledger. Its numbers before 2026-09-13 priced
  batched filter work at live rates; the backfill took that era from the
  verdicts and receipts instead.
- ai_batches.input_tokens, output_tokens, cache_write_tokens, est_cost_usd:
  a batch's totals, now a sum over its calls (model_calls.BATCH_TOTALS).
- job_embeddings.input_tokens, cost_usd: an equal share of a packed request
  that nothing read.

Nothing here writes a row. A column is dropped only once proven empty
(migrations.md): a CHECK that the columns are NULL is added NOT VALID (catalog
only) and validated, a scan under SHARE UPDATE EXCLUSIVE that lets reads and
writes continue. If any row still holds a value, VALIDATE raises, nothing is
dropped, and the container does not start, which is the safe failure. The
drops are catalog only. Every image that names these must be gone from the
fleet first.

Downgrade restores the empty structures, not their contents: the contents
are in model_calls.

Revision ID: c0acfe30a567
Revises: 859638ba54ca
Create Date: 2026-10-10 02:20:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c0acfe30a567"
down_revision: str | None = "859638ba54ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COPIES = {
    "ai_batches": ("input_tokens", "output_tokens", "cache_write_tokens", "est_cost_usd"),
    "job_embeddings": ("input_tokens", "cost_usd"),
}


def upgrade() -> None:
    for table, columns in _COPIES.items():
        guard = f"ck_{table}_usage_cleared"
        nulls = " AND ".join(f"{c} IS NULL" for c in columns)
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '10s'")
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {guard}")
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {guard} CHECK ({nulls}) NOT VALID")
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {guard}")
        for column in columns:
            op.drop_column(table, column)
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {guard}")
    op.execute("DROP TABLE IF EXISTS api_usage")


def downgrade() -> None:
    op.add_column("job_embeddings", sa.Column("cost_usd", sa.Numeric(14, 10), nullable=True))
    op.add_column("job_embeddings", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("ai_batches", sa.Column("est_cost_usd", sa.Numeric(12, 6), nullable=True))
    op.add_column("ai_batches", sa.Column("cache_write_tokens", sa.BigInteger(), nullable=True))
    op.add_column("ai_batches", sa.Column("input_tokens", sa.BigInteger(), nullable=True))
    op.add_column("ai_batches", sa.Column("output_tokens", sa.BigInteger(), nullable=True))
    op.create_table(
        "api_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("key_source", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "completion_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("total_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("cached_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("batched", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("managed_board_id", sa.BigInteger(), nullable=True),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "user_id IS NULL OR managed_board_id IS NULL", name="ck_api_usage_single_subject"
        ),
        sa.ForeignKeyConstraint(["managed_board_id"], ["managed_boards.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_api_usage_user_created", "api_usage", ["user_id", "created_at"])
    op.create_index("idx_api_usage_purpose", "api_usage", ["purpose", "created_at"])
    op.create_index(
        "idx_api_usage_managed_board_created", "api_usage", ["managed_board_id", "created_at"]
    )
