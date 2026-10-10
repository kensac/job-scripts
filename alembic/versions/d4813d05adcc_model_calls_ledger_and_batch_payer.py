"""model_calls: one row per paid provider request, and who pays for a batch

The expand step of the cost ledger (docs/agents/architecture-migration.md,
"The ledger of paid model calls"). Nothing reads the table yet; api_usage and
the copies on ai_queries, ai_batches and review_gate_outcomes are still
written and still read.

ai_batches.payer is recorded at submission by whoever submits, so the receipt
checkpoint can write a batch item's row without inferring a payer from a task
payload. NULL is a batch submitted before this, not the fleet. The CHECK
scans ai_batches once (10,464 rows on 2026-10-10) and every existing row
passes it, since payer is NULL on all of them.

Revision ID: d4813d05adcc
Revises: db0c14af6775
Create Date: 2026-10-10 00:22:49.894932

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4813d05adcc"
down_revision: str | None = "db0c14af6775"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("managed_board_id", sa.BigInteger(), nullable=True),
        sa.Column("key_source", sa.Text(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("provider_batch_id", sa.Text(), nullable=True),
        sa.Column("custom_id", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.CheckConstraint(
            "(provider_batch_id IS NULL) = (custom_id IS NULL)", name="ck_model_calls_batch_item"
        ),
        sa.CheckConstraint(
            "user_id IS NULL OR managed_board_id IS NULL", name="ck_model_calls_single_payer"
        ),
        sa.ForeignKeyConstraint(["managed_board_id"], ["managed_boards.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_batch_id", "custom_id", name="uq_model_calls_batch_item"),
    )
    op.create_index("idx_model_calls_created", "model_calls", ["created_at"], unique=False)
    op.create_index(
        "idx_model_calls_managed_board_created",
        "model_calls",
        ["managed_board_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_model_calls_user_created", "model_calls", ["user_id", "created_at"], unique=False
    )
    op.add_column("ai_batches", sa.Column("payer", sa.Text(), nullable=True))
    op.add_column("ai_batches", sa.Column("payer_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_ai_batches_payer",
        "ai_batches",
        "payer IN ('fleet', 'user', 'managed_board') AND (payer = 'fleet') = (payer_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_batches_payer", "ai_batches", type_="check")
    op.drop_column("ai_batches", "payer_id")
    op.drop_column("ai_batches", "payer")
    op.drop_index("idx_model_calls_user_created", table_name="model_calls")
    op.drop_index("idx_model_calls_managed_board_created", table_name="model_calls")
    op.drop_index("idx_model_calls_created", table_name="model_calls")
    op.drop_table("model_calls")
