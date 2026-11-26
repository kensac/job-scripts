"""batch_result_receipts: responses held by reference

Revision ID: d8e2b4c6a915
Revises: c5a1f7e9d203
Create Date: 2026-11-26

A receipt's response moves to a verified bundle object (api.ai.batch_results):
`response_sha256` names the member, `response_object` and
`response_object_size` the bundle, and `response` is NULL. The four are set
together or not at all (ck_batch_result_receipts_response_whole), added NOT
VALID and then validated, so the scan of the live table runs under SHARE
UPDATE EXCLUSIVE and writes continue.

On 2026-11-26 the table held 2,390,844 receipts in 1,971 MB, 99.95% of them
consumed. A response averaged 430 bytes inline: 107 of text, 261 of usage.

`finish_reason` and `output_tokens` stay on the row because the job profile
report aggregates them over every classify_job_profiles receipt (85,052 on
2026-11-26), which would otherwise read every bundle. model_calls holds no
per-item row for those receipts, so it cannot stand in.

idx_batch_result_receipts_inline is how tasks.receipt_responses finds the
rows still inline, oldest first; once moved it holds only what a roll leaves.
Built CONCURRENTLY under a lock timeout after dropping an INVALID leftover
(bd1e66f153c3). The move rewrites millions of rows, so the table takes the
bulk-update autovacuum scale factors the other such tables use (jobs,
listings, batch_requests, ai_queries) first; ALTER TABLE ... SET rewrites
nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e2b4c6a915"
down_revision: str | None = "c5a1f7e9d203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "batch_result_receipts"
_INDEX = "idx_batch_result_receipts_inline"
_CHECK = "ck_batch_result_receipts_response_whole"
_INVALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = :name AND NOT i.indisvalid"
)
_AUTOVACUUM = "autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.01"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("response_sha256", sa.LargeBinary(), nullable=True))
    op.add_column(_TABLE, sa.Column("response_object", sa.LargeBinary(), nullable=True))
    op.add_column(_TABLE, sa.Column("response_object_size", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("finish_reason", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.alter_column(_TABLE, "response", nullable=True)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CHECK} CHECK ("
        "(response IS NULL) = (response_sha256 IS NOT NULL) "
        "AND (response_sha256 IS NULL) = (response_object IS NULL) "
        "AND (response_object IS NULL) = (response_object_size IS NULL)) NOT VALID"
    )
    op.execute(f"ALTER TABLE {_TABLE} SET ({_AUTOVACUUM})")
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CHECK}")
        # A judgment, not a measurement, as in bd1e66f153c3: a start can afford
        # a minute for the transactions in flight, and past that the wait is
        # not ending.
        op.execute("SET lock_timeout = '60s'")
        if op.get_bind().execute(_INVALID, {"name": _INDEX}).first():
            op.execute(f"DROP INDEX CONCURRENTLY {_INDEX}")
        op.create_index(
            _INDEX,
            _TABLE,
            ["received_at"],
            unique=False,
            postgresql_where=sa.text("response IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    held = (
        op.get_bind()
        .execute(sa.text(f"SELECT 1 FROM {_TABLE} WHERE response IS NULL LIMIT 1"))
        .first()
    )
    if held:
        raise RuntimeError(
            "receipts hold responses by reference; an older image cannot read them"
        )
    with op.get_context().autocommit_block():
        op.drop_index(_INDEX, table_name=_TABLE, postgresql_concurrently=True, if_exists=True)
    op.execute(
        f"ALTER TABLE {_TABLE} RESET (autovacuum_vacuum_scale_factor, "
        "autovacuum_analyze_scale_factor)"
    )
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CHECK}")
    op.alter_column(_TABLE, "response", nullable=False)
    for column in (
        "output_tokens",
        "finish_reason",
        "response_object_size",
        "response_object",
        "response_sha256",
    ):
        op.drop_column(_TABLE, column)
