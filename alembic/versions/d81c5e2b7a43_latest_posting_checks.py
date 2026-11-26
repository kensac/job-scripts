"""latest_posting_checks: each posting's latest answer to each posting check

A projection of `verdicts`: per job and check (every check but custom
filters), the answer with the highest id for the job's url. Scans over the
catalog read a posting's latest closed and clearance answers once per
candidate, an index probe into ai_queries each: 461,746 probes and 11.6 s
for one month of available postings on a production copy (2026-11-26),
against 0.8 s hashing a table of latest answers. Keyed by job id: a url key
made the table and its indexes 606 MB.

Writers of answers and postings never touch the projection. A statement-level
trigger appends the url of every answer or posting a write touches to
latest_posting_checks_dirty, which has no unique key, so the answer and
ingest paths take no row lock they did not take before and cannot deadlock
on it. tasks.latest_posting_checks is the projection's only writer: it folds
the dirty urls in, under one lock and one snapshot, and clears what it
folded. A reader reads a url with a dirty row live from `verdicts` and every
other url from the projection, so it is exact whatever the fold has done:
an answer and its dirty row commit together, and a fold writes the
projection and clears the dirty row together. The pattern is the ledger
rollup's (d4428913f75e, admin-query-reads.md).

The tables start empty and nothing reads them yet: the task fills the
projection, and readers move to it in a later release once it is verified
complete. A data copy here would run behind the schema lock at every
container start. Creating the foreign key and the triggers takes SHARE ROW
EXCLUSIVE on jobs and ai_queries for a moment, so the whole migration runs
under the lock_timeout bd1e66f153c3 states. Additive.

Revision ID: d81c5e2b7a43
Revises: d8e2b4c6a915
Create Date: 2026-11-26 08:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d81c5e2b7a43"
down_revision: str | None = "d8e2b4c6a915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every answer row that can be, or stop being, a posting check's answer: all
# but custom filters, which are keyed by prompt. By exclusion, so a newly
# registered posting check is followed without a migration; the fold reads
# `verdicts` for what counts.
_FUNCTIONS = """
CREATE OR REPLACE FUNCTION latest_posting_checks_dirty_answers() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    INSERT INTO latest_posting_checks_dirty (url)
    SELECT DISTINCT url FROM new_rows
    WHERE url IS NOT NULL AND check_type IS DISTINCT FROM 'custom';
  END IF;
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    INSERT INTO latest_posting_checks_dirty (url)
    SELECT DISTINCT url FROM old_rows
    WHERE url IS NOT NULL AND check_type IS DISTINCT FROM 'custom';
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION latest_posting_checks_dirty_jobs() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO latest_posting_checks_dirty (url) SELECT url FROM new_rows;
  RETURN NULL;
END $$;
"""

_TRIGGERS = """
CREATE TRIGGER latest_posting_checks_dirty_insert AFTER INSERT ON ai_queries
  REFERENCING NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION latest_posting_checks_dirty_answers();
CREATE TRIGGER latest_posting_checks_dirty_update AFTER UPDATE ON ai_queries
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION latest_posting_checks_dirty_answers();
CREATE TRIGGER latest_posting_checks_dirty_delete AFTER DELETE ON ai_queries
  REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION latest_posting_checks_dirty_answers();
CREATE TRIGGER latest_posting_checks_dirty_jobs AFTER INSERT ON jobs
  REFERENCING NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION latest_posting_checks_dirty_jobs();
"""

_TRIGGER_TABLES = (
    ("latest_posting_checks_dirty_insert", "ai_queries"),
    ("latest_posting_checks_dirty_update", "ai_queries"),
    ("latest_posting_checks_dirty_delete", "ai_queries"),
    ("latest_posting_checks_dirty_jobs", "jobs"),
)


def upgrade() -> None:
    # bd1e66f153c3's bound: a start can afford a minute for the transactions
    # in flight, and past that the wait is not ending. Set before the foreign
    # key, which locks jobs too.
    op.execute("SET LOCAL lock_timeout = '60s'")
    op.create_table(
        "latest_posting_checks",
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("check_type", sa.Text(), nullable=False),
        sa.Column("verdict_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "check_type"),
    )
    op.create_table(
        "latest_posting_checks_dirty",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_latest_posting_checks_dirty_url", "latest_posting_checks_dirty", ["url"])
    op.execute(_FUNCTIONS)
    op.execute(_TRIGGERS)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '60s'")
    for name, table in _TRIGGER_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS latest_posting_checks_dirty_answers()")
    op.execute("DROP FUNCTION IF EXISTS latest_posting_checks_dirty_jobs()")
    op.drop_table("latest_posting_checks_dirty")
    op.drop_table("latest_posting_checks")
