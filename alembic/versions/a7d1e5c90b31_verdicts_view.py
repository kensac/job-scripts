"""verdicts: the decided answers in ai_queries, under their own name

ai_queries holds three kinds of record: page text (check_type 'content'),
model answers about a posting, and the usage of each call. A reader asking
"what was decided about this url" had to restate which rows are answers
(status passed or rejected, on a check rather than page text) in each of
about fifty queries. The view states it once.

It names page text as the exclusion rather than listing the checks, so a
check registered in core.checks.POSTING_CHECKS is a verdict here without a
migration. Failed attempts are not verdicts: they are calls, and stay in
ai_queries for the readers that count calls.

It is the name readers use, so the storage behind it can change without
them. A plain view is inlined by the planner, so a reader's predicate still
reaches the partial indexes idx_ai_queries_latest_verdict and
idx_ai_queries_latest_custom, whose predicates the view's own WHERE implies.

Creating it takes ACCESS SHARE on ai_queries only.

Revision ID: a7d1e5c90b31
Revises: bc1978f126b4
Create Date: 2026-10-09 23:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7d1e5c90b31"
down_revision: str | None = "bc1978f126b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW verdicts AS
        SELECT id, created_at, url, check_type, status, reason, model,
               reasoning_effort, filter_name, prompt_hash, company, job_title,
               instructions, instructions_id, parsed_json, config_name, batch_id,
               worker, request_sha256
        FROM ai_queries
        WHERE check_type <> 'content'
          AND status IN ('passed', 'rejected')
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS verdicts")
