"""verdicts: an answer read off a page that is not the posting is no evidence

An answer that judged a fetch page_text_rejections names (a maintenance
notice, a career site's shell, a cookie wall: 0838a60ed6e9) judged a page
that was not the posting. On 2026-11-27 the Workday maintenance page alone
carried 364 custom rejections, 67 closed checks read as open and 70
clearance passes. verdicts, which every reader of a decided answer goes
through (core.verdict_reads, core.latest_posting_checks), leaves such an
answer out, so the posting reads as not yet answered for that check and is
asked again on its own text.

One kind stays: a closed check's rejection. A page that says the posting is
gone ("This job is no longer available") is shared across roles and names
none, so it is rejected as posting text, and it is still the evidence the
closed check reads.

CREATE OR REPLACE keeps the view's columns, so nothing that selects from it
changes, and nothing is rewritten.

Revision ID: 835b21f59b79
Revises: 0838a60ed6e9
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "835b21f59b79"
down_revision: str | None = "0838a60ed6e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SELECT = """
    SELECT id, created_at, url, check_type, status, reason, model,
           reasoning_effort, filter_name, prompt_hash, company, job_title,
           instructions_id, parsed_json, config_name, batch_id,
           worker, request_sha256
    FROM ai_queries
    WHERE check_type <> 'content'
      AND status IN ('passed', 'rejected')
"""
_ON_THE_POSTING = """
      AND (page_fetch_id IS NULL
           OR (check_type = 'closed' AND status = 'rejected')
           OR NOT EXISTS (SELECT 1 FROM page_text_rejections r
                          WHERE r.fetch_id = ai_queries.page_fetch_id))
"""


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE VIEW verdicts AS {_SELECT} {_ON_THE_POSTING}")


def downgrade() -> None:
    op.execute(f"CREATE OR REPLACE VIEW verdicts AS {_SELECT}")
