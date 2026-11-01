"""ledger_rows: the fetch arm is flattened, so it reads its indexes

Postgres flattens a UNION ALL arm into the outer query only when the arm has
no WHERE of its own. The fetch arm had one (method <> 'verification',
95018279350d), so every read of the view planned that arm as a sealed
subquery: a newest-first page sorted all 1.4M fetches instead of walking
page_fetches_pkey. On production (2026-11-01) the Responses page's
`ORDER BY id DESC LIMIT 50` took 2.0 s, of which the answers arm was 0.3 ms.

The arms now carry which one they are, and the filter moves to the outer
query: an answer passes it by its arm, a fetch by its method. A filter on the
outer query is pushed into each arm, where it folds to true for the answers
and to `method <> 'verification'` for the fetches, so both arms flatten.
The same page reads in 1.3 ms. The rows and the columns are unchanged; the
marker is not one of the view's columns.

Revision ID: 58e4e8b6b54d
Revises: 7d3a9c2e5b18
Create Date: 2026-11-01 01:32:19.291001

"""

from collections.abc import Sequence

from alembic import op

revision: str = "58e4e8b6b54d"
down_revision: str | None = "7d3a9c2e5b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# core.answer_inputs.sql('q', 'f.content') as of a0e5b60bf58d.
_INPUT = """(CASE WHEN (q.check_type = 'custom' AND COALESCE(q.config_name, '') NOT IN ('manual', 'explain')) THEN ('Company: ' || COALESCE(q.company, '') || E'\\nJob Title: ' || COALESCE(q.job_title, '') || E'\\n\\nJob Content:\\n') || left(f.content, CASE WHEN COALESCE(q.config_name, '') IN ('manual', 'explain') THEN 60000 WHEN (q.check_type = 'custom' AND COALESCE(q.config_name, '') NOT IN ('manual', 'explain')) AND q.config_name = 'verify-batch' THEN 20000 ELSE 2147483647 END) ELSE left(f.content, CASE WHEN COALESCE(q.config_name, '') IN ('manual', 'explain') THEN 60000 WHEN (q.check_type = 'custom' AND COALESCE(q.config_name, '') NOT IN ('manual', 'explain')) AND q.config_name = 'verify-batch' THEN 20000 ELSE 2147483647 END) END)"""

# The first answer naming a call carries its numbers, its siblings zeros.
_FIRST = (
    "NOT EXISTS (SELECT 1 FROM ai_queries o "
    "WHERE o.model_call_id = q.model_call_id AND o.id < q.id)"
)

_COLUMNS = (
    "id, created_at, config_name, url, check_type, status, reason, model, "
    "reasoning_effort, filter_name, prompt_hash, company, job_title, instructions_id, "
    "input_content, parsed_json, prompt_tokens, completion_tokens, total_tokens, "
    "cached_tokens, cache_write_tokens, reasoning_tokens, duration_ms, error, cost_usd, "
    "worker, batch_id"
)


def _usage(column: str, sibling: str) -> str:
    type_ = "numeric" if column == "cost_usd" else "bigint"
    return (
        f"CASE WHEN q.model_call_id IS NULL THEN NULL::{type_} "
        f"WHEN {_FIRST} THEN (SELECT m.{column} FROM model_calls m WHERE m.id = q.model_call_id) "
        f"ELSE {sibling} END AS {column}"
    )


def _arms(marked: bool) -> tuple[str, str]:
    """The answers arm and the fetch arm, as a0e5b60bf58d wrote them, each
    with its arm's name as a last column when `marked`."""
    answer_arm = ", 'answer'::text AS arm" if marked else ""
    fetch_arm = ", 'fetch'::text" if marked else ""
    cost_sibling = "(SELECT m.cost_usd * 0 FROM model_calls m WHERE m.id = q.model_call_id)"
    answers = f"""
        SELECT q.id, q.created_at, q.config_name, q.url, q.check_type, q.status, q.reason,
               q.model, q.reasoning_effort, q.filter_name, q.prompt_hash, q.company,
               q.job_title, q.instructions_id,
               (SELECT {_INPUT} FROM page_fetches f WHERE f.id = q.page_fetch_id) AS input_content,
               q.parsed_json,
               {_usage("prompt_tokens", "0")},
               {_usage("completion_tokens", "0")},
               {_usage("total_tokens", "0")},
               {_usage("cached_tokens", "0")},
               {_usage("cache_write_tokens", "0")},
               {_usage("reasoning_tokens", "0")},
               {_usage("duration_ms", "NULL")},
               q.error,
               {_usage("cost_usd", cost_sibling)},
               q.worker, q.batch_id{answer_arm}
        FROM ai_queries q"""
    fetches = f"""
        SELECT id, created_at, 'content-cache', url, 'content', status, method,
               NULL, NULL, NULL, NULL, NULL, NULL,
               NULL::bigint, content, NULL,
               NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint,
               NULL::bigint, NULL::bigint, NULL::bigint, NULL, NULL::numeric,
               worker, NULL{fetch_arm}
        FROM page_fetches"""
    return answers, fetches


def upgrade() -> None:
    answers, fetches = _arms(marked=True)
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(
        f"""
        CREATE OR REPLACE VIEW ledger_rows AS
        SELECT {_COLUMNS} FROM ({answers}
            UNION ALL{fetches}
        ) arms
        WHERE arm = 'answer' OR reason <> 'verification'
        """
    )


def downgrade() -> None:
    answers, fetches = _arms(marked=False)
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(
        f"""
        CREATE OR REPLACE VIEW ledger_rows AS{answers}
        UNION ALL{fetches}
        WHERE method <> 'verification'
        """
    )
