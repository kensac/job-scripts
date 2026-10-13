"""ledger rollup tables

answer_usage_hourly holds the answer ledger summed per UTC hour, so a read of
a month or a lifetime adds up a few thousand groups instead of aggregating two
million wide rows: /admin/stats took 15 to 33 s and the spend cuts 15 s each
on production (2026-10-13). api.ledger_rollup reads an hour from it only when
the hour is older than ledger_rollup_state.computed_until and no write since
has logged it in ledger_dirty_hours; everything else is aggregated from the
source tables when read, so a read is exact whatever the refresh has done.

The triggers append the UTC hour of every row a write to ai_queries or
page_fetches touches, and for ai_queries and model_calls the hours of every
answer naming the same call (which answer is a call's first, the one that
carries its usage, can change). The log is append-only, so concurrent writers
never wait on one another; tasks.ledger_rollup deletes what it recomputes.
Statement-level triggers with transition tables: one insert per statement and
hour, not per row. CREATE TRIGGER takes SHARE ROW EXCLUSIVE for a moment, so
it runs under a lock_timeout. The tables start empty: the first refresh fills
them (a task, not this migration).

Revision ID: d4428913f75e
Revises: 5a04926d6434
Create Date: 2026-10-13 00:17:47.814542

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4428913f75e"
down_revision: str | None = "5a04926d6434"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HOUR = "date_trunc('hour', {col}, 'UTC')"

_FUNCTIONS = f"""
CREATE OR REPLACE FUNCTION ledger_dirty_answers() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    INSERT INTO ledger_dirty_hours (hour)
    SELECT DISTINCT {_HOUR.format(col="created_at")} FROM new_rows;
  END IF;
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    INSERT INTO ledger_dirty_hours (hour)
    SELECT DISTINCT {_HOUR.format(col="created_at")} FROM old_rows;
    INSERT INTO ledger_dirty_hours (hour)
    SELECT DISTINCT {_HOUR.format(col="o.created_at")} FROM ai_queries o
    WHERE o.model_call_id IN (SELECT model_call_id FROM old_rows WHERE model_call_id IS NOT NULL);
  END IF;
  IF TG_OP = 'UPDATE' THEN
    INSERT INTO ledger_dirty_hours (hour)
    SELECT DISTINCT {_HOUR.format(col="o.created_at")} FROM ai_queries o
    WHERE o.model_call_id IN (SELECT model_call_id FROM new_rows WHERE model_call_id IS NOT NULL);
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION ledger_dirty_calls() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO ledger_dirty_hours (hour)
  SELECT DISTINCT {_HOUR.format(col="o.created_at")} FROM ai_queries o
  WHERE o.model_call_id IN (SELECT id FROM old_rows);
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION ledger_dirty_fetches() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    INSERT INTO ledger_dirty_hours (hour)
    SELECT DISTINCT {_HOUR.format(col="created_at")} FROM new_rows;
  END IF;
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    INSERT INTO ledger_dirty_hours (hour)
    SELECT DISTINCT {_HOUR.format(col="created_at")} FROM old_rows;
  END IF;
  RETURN NULL;
END $$;
"""

_TRIGGERS = """
CREATE TRIGGER ledger_dirty_answers_insert AFTER INSERT ON ai_queries
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION ledger_dirty_answers();
CREATE TRIGGER ledger_dirty_answers_update AFTER UPDATE ON ai_queries
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION ledger_dirty_answers();
CREATE TRIGGER ledger_dirty_answers_delete AFTER DELETE ON ai_queries
  REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION ledger_dirty_answers();
CREATE TRIGGER ledger_dirty_calls_update AFTER UPDATE ON model_calls
  REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION ledger_dirty_calls();
CREATE TRIGGER ledger_dirty_calls_delete AFTER DELETE ON model_calls
  REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION ledger_dirty_calls();
CREATE TRIGGER ledger_dirty_fetches_insert AFTER INSERT ON page_fetches
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION ledger_dirty_fetches();
CREATE TRIGGER ledger_dirty_fetches_update AFTER UPDATE ON page_fetches
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION ledger_dirty_fetches();
CREATE TRIGGER ledger_dirty_fetches_delete AFTER DELETE ON page_fetches
  REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION ledger_dirty_fetches();
"""

_TRIGGER_NAMES = {
    "ai_queries": ("insert", "update", "delete"),
    "model_calls": ("update", "delete"),
    "page_fetches": ("insert", "update", "delete"),
}


def upgrade() -> None:
    op.create_table(
        "answer_usage_hourly",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("hour", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("arm", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("check_type", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("config_name", sa.Text(), nullable=True),
        sa.Column("prompt_hash", sa.Text(), nullable=True),
        sa.Column("batched", sa.Boolean(), nullable=False),
        sa.Column("answers", sa.BigInteger(), nullable=False),
        sa.Column("unpriced", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_unknown", sa.BigInteger(), nullable=False),
        sa.Column("joint_call_rows", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(), nullable=True),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=True),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=True),
        sa.Column("first_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("arm IN ('answer', 'fetch')", name="ck_answer_usage_hourly_arm"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_answer_usage_hourly_hour", "answer_usage_hourly", ["hour"], unique=False)
    op.create_table(
        "ledger_dirty_hours",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("hour", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_ledger_dirty_hours_hour", "ledger_dirty_hours", ["hour"], unique=False)
    op.create_table(
        "ledger_rollup_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("computed_until", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_ledger_rollup_state_single"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(_FUNCTIONS)
    op.execute(_TRIGGERS)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    for table, events in _TRIGGER_NAMES.items():
        prefix = {"ai_queries": "answers", "model_calls": "calls", "page_fetches": "fetches"}[table]
        for event in events:
            op.execute(f"DROP TRIGGER IF EXISTS ledger_dirty_{prefix}_{event} ON {table}")
    for fn in ("ledger_dirty_answers", "ledger_dirty_calls", "ledger_dirty_fetches"):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
    op.drop_table("ledger_rollup_state")
    op.drop_index("idx_ledger_dirty_hours_hour", table_name="ledger_dirty_hours")
    op.drop_table("ledger_dirty_hours")
    op.drop_index("idx_answer_usage_hourly_hour", table_name="answer_usage_hourly")
    op.drop_table("answer_usage_hourly")
