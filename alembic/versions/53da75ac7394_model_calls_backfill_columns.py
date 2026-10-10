"""model_calls: the columns the backfill needs to say what it knows

The backfill (api.model_calls, task backfill_model_calls) copies calls from
the records that were right in their era. Those records know less than a
call written at the time, and the table has to say so rather than guess:

- payer: 'fleet', 'user' or 'managed_board'. NULL is a call nothing recorded
  a payer for, which is not the fleet. Rows already written are given the
  payer their ids say: both NULL was the fleet when they were written.
- key_source and reasoning_tokens become nullable: api_usage kept no
  reasoning tokens, and a verdict row did not say whose key.
- batched: a call can be batched with no provider batch id (the 2026-09-07
  experiment results).
- requests: a batch that left no per-request record is one row for the batch.
- source, source_id: which table a backfilled row came from, and its id, so a
  resumed backfill cannot copy a row twice. 'call' is written at call time.

The table is small (written since d4813d05adcc), so the fill and SET NOT
NULL are one short transaction.

Revision ID: 53da75ac7394
Revises: 5a7c2e9d1b40
Create Date: 2026-10-10 01:10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "53da75ac7394"
down_revision: str | None = "5a7c2e9d1b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_calls", sa.Column("payer", sa.Text(), nullable=True))
    op.add_column("model_calls", sa.Column("batched", sa.Boolean(), nullable=True))
    op.add_column(
        "model_calls",
        sa.Column("requests", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "model_calls",
        sa.Column("source", sa.Text(), server_default=sa.text("'call'"), nullable=False),
    )
    op.add_column("model_calls", sa.Column("source_id", sa.BigInteger(), nullable=True))
    op.execute(
        "UPDATE model_calls SET batched = provider_batch_id IS NOT NULL, "
        "payer = CASE WHEN user_id IS NOT NULL THEN 'user' "
        "WHEN managed_board_id IS NOT NULL THEN 'managed_board' ELSE 'fleet' END"
    )
    op.alter_column("model_calls", "batched", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("model_calls", "key_source", existing_type=sa.TEXT(), nullable=True)
    op.alter_column("model_calls", "reasoning_tokens", existing_type=sa.BIGINT(), nullable=True)
    op.drop_constraint("ck_model_calls_batch_item", "model_calls", type_="check")
    op.create_check_constraint(
        "ck_model_calls_batch_item",
        "model_calls",
        "custom_id IS NULL OR provider_batch_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_model_calls_batch_is_batched", "model_calls", "provider_batch_id IS NULL OR batched"
    )
    op.create_check_constraint(
        "ck_model_calls_requests",
        "model_calls",
        "requests = 1 OR (custom_id IS NULL AND provider_batch_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_model_calls_payer",
        "model_calls",
        "(payer IS NULL OR payer IN ('fleet', 'user', 'managed_board')) "
        "AND (user_id IS NULL OR payer = 'user') "
        "AND (managed_board_id IS NULL OR payer = 'managed_board')",
    )
    op.create_check_constraint(
        "ck_model_calls_source",
        "model_calls",
        "source IN ('call', 'verdict', 'receipt', 'batch', 'usage')",
    )
    op.create_index(
        "uq_model_calls_batch_aggregate",
        "model_calls",
        ["provider_batch_id"],
        unique=True,
        postgresql_where=sa.text("custom_id IS NULL AND provider_batch_id IS NOT NULL"),
    )
    op.create_index(
        "uq_model_calls_source_row",
        "model_calls",
        ["source", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM model_calls WHERE source <> 'call'")
    op.drop_index("uq_model_calls_source_row", table_name="model_calls")
    op.drop_index("uq_model_calls_batch_aggregate", table_name="model_calls")
    for name in (
        "ck_model_calls_source",
        "ck_model_calls_payer",
        "ck_model_calls_requests",
        "ck_model_calls_batch_is_batched",
        "ck_model_calls_batch_item",
    ):
        op.drop_constraint(name, "model_calls", type_="check")
    op.create_check_constraint(
        "ck_model_calls_batch_item",
        "model_calls",
        "(provider_batch_id IS NULL) = (custom_id IS NULL)",
    )
    op.alter_column("model_calls", "reasoning_tokens", existing_type=sa.BIGINT(), nullable=False)
    op.alter_column("model_calls", "key_source", existing_type=sa.TEXT(), nullable=False)
    op.drop_column("model_calls", "source_id")
    op.drop_column("model_calls", "source")
    op.drop_column("model_calls", "requests")
    op.drop_column("model_calls", "batched")
    op.drop_column("model_calls", "payer")
