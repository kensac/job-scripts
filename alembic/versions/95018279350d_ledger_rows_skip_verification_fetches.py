"""ledger_rows: a fetch made from an answer's copy is not a second row

The move of page fetches (tasks/page_fetch_move.py) inserts, for each url
whose only page text is a copy on a closed or clearance answer, the copy a
reader picks as a fetch with method 'verification' and the answer's id. That
answer is still a row of ai_queries, so ledger_rows would list it twice and
board spend would count 11,272 calls that never happened (2026-10-10). The
fetch arm leaves those out.

Revision ID: 95018279350d
Revises: 5460c0343629
Create Date: 2026-10-10 00:18:35.395748

"""

from collections.abc import Sequence

from alembic import op

revision: str = "95018279350d"
down_revision: str | None = "5460c0343629"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGER_ROWS = """
    CREATE OR REPLACE VIEW ledger_rows AS
    SELECT id, created_at, config_name, url, check_type, status, reason, model,
           reasoning_effort, filter_name, prompt_hash, company, job_title,
           instructions, instructions_id, input_content, parsed_json,
           prompt_tokens, completion_tokens, total_tokens, cached_tokens,
           cache_write_tokens, reasoning_tokens, duration_ms, error, cost_usd,
           worker, batch_id
    FROM ai_queries
    UNION ALL
    SELECT id, created_at, 'content-cache', url, 'content', status, method,
           NULL, NULL, NULL, NULL, NULL, NULL,
           NULL, NULL::bigint, content, NULL,
           NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint,
           NULL::bigint, NULL::bigint, NULL::bigint, NULL, NULL::numeric,
           worker, NULL
    FROM page_fetch_rows
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(_LEDGER_ROWS + "    WHERE method <> 'verification'\n")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(_LEDGER_ROWS)
