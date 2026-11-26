"""latest_posting_checks: each posting's latest answer to each posting check

A projection of `verdicts`: per job and check (everything but custom
filters), the answer with the highest id for the job's url. Scans over the
catalog read a posting's latest closed and clearance answers once per
candidate, an index probe into ai_queries each: 461,746 probes and 11.6 s
for one month of available postings on a production copy (2026-11-26),
against 0.8 s hashing this table's passing rows. Keyed by job id: a url key
made the table and its indexes 606 MB.

Exact in the same transaction, so a reader never sees it behind:
- an insert into ai_queries upserts each named (job, check), keeping the
  higher answer id whatever order the inserts commit in;
- an update or delete in ai_queries recomputes every (job, check) its old
  or new rows named from `verdicts`;
- an insert into jobs seeds the new job from the answers its url already
  has.
Statement-level with transition tables, one statement per write and not one
per row; a jobs upsert that inserted nothing joins an empty table. Every
upsert takes its rows in (job_id, check_type) order, so two writers never
lock them in opposite orders.

The table starts empty and nothing reads it yet: tasks.latest_posting_checks
fills it and keeps checking it, and readers move to it in a later release
once it is verified complete. A data copy here would run behind the schema
lock at every container start. CREATE TRIGGER takes SHARE ROW EXCLUSIVE on
ai_queries and jobs for a moment, so it runs under a lock_timeout, the bound
bd1e66f153c3 states. Additive.

Revision ID: d81c5e2b7a43
Revises: c5a1f7e9d203
Create Date: 2026-11-26 08:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d81c5e2b7a43"
down_revision: str | None = "c5a1f7e9d203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The rows `verdicts` holds that this projects: every check but a custom
# filter, which is keyed by prompt and has its own index. By exclusion, so a
# newly registered posting check is projected without a migration.
_PROJECTED = "{t}.check_type <> 'custom'"

_FUNCTIONS = f"""
CREATE OR REPLACE FUNCTION latest_posting_checks_answers() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  key_jobs bigint[];
  key_checks text[];
BEGIN
  IF TG_OP = 'INSERT' THEN
    INSERT INTO latest_posting_checks (job_id, check_type, verdict_id, status)
    SELECT DISTINCT ON (j.id, n.check_type) j.id, n.check_type, n.id, n.status
    FROM new_rows n JOIN jobs j ON j.url = n.url
    WHERE {_PROJECTED.format(t="n")} AND n.check_type <> 'content'
      AND n.status IN ('passed', 'rejected')
    ORDER BY j.id, n.check_type, n.id DESC
    ON CONFLICT (job_id, check_type) DO UPDATE
      SET verdict_id = EXCLUDED.verdict_id, status = EXCLUDED.status
      WHERE latest_posting_checks.verdict_id < EXCLUDED.verdict_id;
    RETURN NULL;
  END IF;
  -- An update or a delete can make an older answer the latest, or leave
  -- none: recompute each key the changed rows named, from verdicts as this
  -- statement left them.
  IF TG_OP = 'DELETE' THEN
    SELECT array_agg(j.id), array_agg(o.check_type) INTO key_jobs, key_checks
    FROM old_rows o JOIN jobs j ON j.url = o.url
    WHERE {_PROJECTED.format(t="o")};
  ELSE
    SELECT array_agg(k.job_id), array_agg(k.check_type) INTO key_jobs, key_checks
    FROM (SELECT j.id AS job_id, o.check_type FROM old_rows o JOIN jobs j ON j.url = o.url
          WHERE {_PROJECTED.format(t="o")}
          UNION
          SELECT j.id, n.check_type FROM new_rows n JOIN jobs j ON j.url = n.url
          WHERE {_PROJECTED.format(t="n")}) k;
  END IF;
  IF key_jobs IS NULL THEN
    RETURN NULL;
  END IF;
  WITH keys AS (
    SELECT DISTINCT job_id, check_type FROM unnest(key_jobs, key_checks) AS k(job_id, check_type)
  ), fresh AS MATERIALIZED (
    SELECT k.job_id, k.check_type, v.id AS verdict_id, v.status
    FROM keys k JOIN jobs j ON j.id = k.job_id
    LEFT JOIN LATERAL (
      SELECT vl.id, vl.status FROM verdicts vl
      WHERE vl.url = j.url AND vl.check_type = k.check_type
      ORDER BY vl.id DESC LIMIT 1) v ON TRUE
  ), gone AS (
    DELETE FROM latest_posting_checks l USING fresh f
    WHERE l.job_id = f.job_id AND l.check_type = f.check_type AND f.verdict_id IS NULL
  )
  INSERT INTO latest_posting_checks (job_id, check_type, verdict_id, status)
  SELECT job_id, check_type, verdict_id, status FROM fresh
  WHERE verdict_id IS NOT NULL
  ORDER BY job_id, check_type
  ON CONFLICT (job_id, check_type) DO UPDATE
    SET verdict_id = EXCLUDED.verdict_id, status = EXCLUDED.status;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION latest_posting_checks_jobs() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO latest_posting_checks (job_id, check_type, verdict_id, status)
  SELECT DISTINCT ON (n.id, v.check_type) n.id, v.check_type, v.id, v.status
  FROM new_rows n JOIN verdicts v ON v.url = n.url
  WHERE {_PROJECTED.format(t="v")}
  ORDER BY n.id, v.check_type, v.id DESC
  ON CONFLICT (job_id, check_type) DO UPDATE
    SET verdict_id = EXCLUDED.verdict_id, status = EXCLUDED.status
    WHERE latest_posting_checks.verdict_id < EXCLUDED.verdict_id;
  RETURN NULL;
END $$;
"""

_TRIGGERS = """
CREATE TRIGGER latest_posting_checks_insert AFTER INSERT ON ai_queries
  REFERENCING NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION latest_posting_checks_answers();
CREATE TRIGGER latest_posting_checks_update AFTER UPDATE ON ai_queries
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION latest_posting_checks_answers();
CREATE TRIGGER latest_posting_checks_delete AFTER DELETE ON ai_queries
  REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION latest_posting_checks_answers();
CREATE TRIGGER latest_posting_checks_jobs_insert AFTER INSERT ON jobs
  REFERENCING NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION latest_posting_checks_jobs();
"""


def upgrade() -> None:
    op.create_table(
        "latest_posting_checks",
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("check_type", sa.Text(), nullable=False),
        sa.Column("verdict_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "check_type"),
    )
    op.execute(_FUNCTIONS)
    op.execute("SET LOCAL lock_timeout = '60s'")
    op.execute(_TRIGGERS)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '60s'")
    for name, table in (
        ("latest_posting_checks_insert", "ai_queries"),
        ("latest_posting_checks_update", "ai_queries"),
        ("latest_posting_checks_delete", "ai_queries"),
        ("latest_posting_checks_jobs_insert", "jobs"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS latest_posting_checks_answers()")
    op.execute("DROP FUNCTION IF EXISTS latest_posting_checks_jobs()")
    op.drop_table("latest_posting_checks")
