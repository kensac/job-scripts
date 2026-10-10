"""ledger_rows reads an answer's input and usage through its fetch and call

An answer that points at the page fetch it judged (page_fetch_id) shows the
input rebuilt from that fetch (core.answer_inputs.sql, frozen here); one that
points at its call (model_call_id) shows the call's numbers. One call can
answer several checks (verification's closed and clearance, and a board's
question in the same request): the first answer naming it shows the call,
its siblings zeros, which is how the writers stored them. An answer without
a pointer shows its stored copy, so nothing moves for answers not yet linked.

From this release the writers store no copy; once tasks.answer_links has
linked an answer, the copy it still holds is the view's value by
construction. The admin ledger, the spend diagnostics, the batch drill-down
and the review gate's cost read this view instead of ai_queries.

Each derived column is a correlated lookup in the select list rather than a
join, so the arm stays one table and is flattened into the outer query; the
admin ledger's skip scans and newest-first pages depend on that. A lookup
runs only for a column that is read.

idx_ai_queries_model_call (ed4c4525c227) serves the "is there an earlier
answer naming this call" probe.

Revision ID: 7cff6a3de670
Revises: ed4c4525c227
Create Date: 2026-10-10 15:10:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "7cff6a3de670"
down_revision: str | None = "ed4c4525c227"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# core.answer_inputs.sql('q', 'f.content') as of this revision.
_INPUT = """(CASE WHEN (q.check_type = 'custom' AND COALESCE(q.config_name, '') NOT IN ('manual', 'explain')) THEN ('Company: ' || COALESCE(q.company, '') || E'\\nJob Title: ' || COALESCE(q.job_title, '') || E'\\n\\nJob Content:\\n') || left(f.content, CASE WHEN COALESCE(q.config_name, '') IN ('manual', 'explain') THEN 60000 WHEN (q.check_type = 'custom' AND COALESCE(q.config_name, '') NOT IN ('manual', 'explain')) AND q.config_name = 'verify-batch' THEN 20000 ELSE 2147483647 END) ELSE left(f.content, CASE WHEN COALESCE(q.config_name, '') IN ('manual', 'explain') THEN 60000 WHEN (q.check_type = 'custom' AND COALESCE(q.config_name, '') NOT IN ('manual', 'explain')) AND q.config_name = 'verify-batch' THEN 20000 ELSE 2147483647 END) END)"""


# Each derived column is a correlated lookup in the select list, never a join
# in FROM: a UNION ALL arm is flattened into the outer query only while its
# FROM is one table, and the admin ledger's skip scans (min() per column) and
# its newest-first pages need it flattened. A lookup runs only when its
# column is read.
_FIRST = (
    "NOT EXISTS (SELECT 1 FROM ai_queries o "
    "WHERE o.model_call_id = q.model_call_id AND o.id < q.id)"
)


def _usage(column: str, sibling: str) -> str:
    return (
        f"CASE WHEN q.model_call_id IS NULL THEN q.{column} "
        f"WHEN {_FIRST} THEN (SELECT m.{column} FROM model_calls m WHERE m.id = q.model_call_id) "
        f"ELSE {sibling} END AS {column}"
    )


_FETCH_ARM = """
    SELECT id, created_at, 'content-cache', url, 'content', status, method,
           NULL, NULL, NULL, NULL, NULL, NULL,
           NULL, NULL::bigint, content, NULL,
           NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint,
           NULL::bigint, NULL::bigint, NULL::bigint, NULL, NULL::numeric,
           worker, NULL
    FROM page_fetches
    WHERE method <> 'verification'
"""

_LEDGER_ROWS = f"""
    CREATE VIEW ledger_rows AS
    SELECT q.id, q.created_at, q.config_name, q.url, q.check_type, q.status, q.reason, q.model,
           q.reasoning_effort, q.filter_name, q.prompt_hash, q.company, q.job_title,
           q.instructions, q.instructions_id,
           CASE WHEN q.page_fetch_id IS NULL THEN q.input_content
                ELSE (SELECT {_INPUT} FROM page_fetches f WHERE f.id = q.page_fetch_id) END
               AS input_content,
           q.parsed_json,
           {_usage("prompt_tokens", "0")},
           {_usage("completion_tokens", "0")},
           {_usage("total_tokens", "0")},
           {_usage("cached_tokens", "0")},
           {_usage("cache_write_tokens", "0")},
           {_usage("reasoning_tokens", "0")},
           {_usage("duration_ms", "NULL")},
           q.error,
           {_usage("cost_usd", "(SELECT m.cost_usd * 0 FROM model_calls m WHERE m.id = q.model_call_id)")},
           q.worker, q.batch_id
    FROM ai_queries q
    UNION ALL
    {_FETCH_ARM}
"""

_LEDGER_ROWS_BEFORE = f"""
    CREATE VIEW ledger_rows AS
    SELECT id, created_at, config_name, url, check_type, status, reason, model,
           reasoning_effort, filter_name, prompt_hash, company, job_title,
           instructions, instructions_id, input_content, parsed_json,
           prompt_tokens, completion_tokens, total_tokens, cached_tokens,
           cache_write_tokens, reasoning_tokens, duration_ms, error, cost_usd,
           worker, batch_id
    FROM ai_queries
    UNION ALL
    {_FETCH_ARM}
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("DROP VIEW ledger_rows")
    op.execute(_LEDGER_ROWS)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("DROP VIEW ledger_rows")
    op.execute(_LEDGER_ROWS_BEFORE)
