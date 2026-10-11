"""drop the copies answers carried

The contract step of phase 8 (docs/agents/architecture-migration.md). An
answer in ai_queries carried what it judged (input_content), its call's usage
(the token counts, duration_ms, cost_usd) and its prompt (instructions).
Each answer points at the page fetch it judged and the call that paid for it
(#944), readers read through those pointers (#953),
tasks.answer_copies emptied the copies (#972; on production run 9514868
cleared 1,955,077 inputs and 1,954,964 usage copies, and run 9541589 found
0 / 0 / 0 left on 2026-10-10), and no code names them (#974). instructions
held 0 values of 2,826,328 since the reference replaced it.

Nothing here can drop data. A CHECK that every one of these columns is NULL
is added NOT VALID (catalog only) and validated: one scan of the heap under
SHARE UPDATE EXCLUSIVE, so reads and writes continue. If any row still holds
a value, VALIDATE raises, nothing is dropped, and the container does not
start, which is the safe failure. Every image that names a column must be
gone from the fleet first.

The views that name them are recreated in the same short transaction:
verdicts without instructions, and ledger_rows without instructions and
without the fallback to a stored copy, so an answer with no fetch or no call
shows none. Dropping a column is catalog only and rewrites nothing; the
space the emptied values held comes back with VACUUM FULL ai_queries.

Downgrade restores the empty columns and the views as #953 left them, not
the values: those are the page fetches and the calls.

Revision ID: a0e5b60bf58d
Revises: d71ea321d632
Create Date: 2026-10-10 22:52:55.842704

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a0e5b60bf58d"
down_revision: str | None = "d71ea321d632"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD = "ck_ai_queries_copies_cleared"
_COPIES: dict[str, sa.types.TypeEngine] = {
    "input_content": sa.Text(),
    "instructions": sa.Text(),
    "prompt_tokens": sa.BigInteger(),
    "completion_tokens": sa.BigInteger(),
    "total_tokens": sa.BigInteger(),
    "cached_tokens": sa.BigInteger(),
    "cache_write_tokens": sa.BigInteger(),
    "reasoning_tokens": sa.BigInteger(),
    "duration_ms": sa.BigInteger(),
    "cost_usd": sa.Numeric(12, 6),
}

# core.answer_inputs.sql('q', 'f.content') as of this revision.
_INPUT = """(CASE WHEN (q.check_type = 'custom' AND COALESCE(q.config_name, '') NOT IN ('manual', 'explain')) THEN ('Company: ' || COALESCE(q.company, '') || E'\\nJob Title: ' || COALESCE(q.job_title, '') || E'\\n\\nJob Content:\\n') || left(f.content, CASE WHEN COALESCE(q.config_name, '') IN ('manual', 'explain') THEN 60000 WHEN (q.check_type = 'custom' AND COALESCE(q.config_name, '') NOT IN ('manual', 'explain')) AND q.config_name = 'verify-batch' THEN 20000 ELSE 2147483647 END) ELSE left(f.content, CASE WHEN COALESCE(q.config_name, '') IN ('manual', 'explain') THEN 60000 WHEN (q.check_type = 'custom' AND COALESCE(q.config_name, '') NOT IN ('manual', 'explain')) AND q.config_name = 'verify-batch' THEN 20000 ELSE 2147483647 END) END)"""

# The first answer naming a call carries its numbers, its siblings zeros.
# Each derived column is a correlated lookup in the select list, never a join
# in FROM, so the arm stays one table and is flattened into the outer query
# (7cff6a3de670).
_FIRST = (
    "NOT EXISTS (SELECT 1 FROM ai_queries o "
    "WHERE o.model_call_id = q.model_call_id AND o.id < q.id)"
)


def _usage(column: str, sibling: str, stored: bool) -> str:
    unlinked = f"q.{column}" if stored else f"NULL::{_TYPES[column]}"
    return (
        f"CASE WHEN q.model_call_id IS NULL THEN {unlinked} "
        f"WHEN {_FIRST} THEN (SELECT m.{column} FROM model_calls m WHERE m.id = q.model_call_id) "
        f"ELSE {sibling} END AS {column}"
    )


_TYPES = {c: "numeric" if c == "cost_usd" else "bigint" for c in _COPIES}


def _ledger_rows(stored: bool) -> str:
    instructions = "q.instructions, " if stored else ""
    fetch_instructions = "NULL, " if stored else ""
    input_content = (
        "CASE WHEN q.page_fetch_id IS NULL THEN q.input_content "
        f"ELSE (SELECT {_INPUT} FROM page_fetches f WHERE f.id = q.page_fetch_id) END"
        if stored
        else f"(SELECT {_INPUT} FROM page_fetches f WHERE f.id = q.page_fetch_id)"
    )
    cost_sibling = "(SELECT m.cost_usd * 0 FROM model_calls m WHERE m.id = q.model_call_id)"
    return f"""
        CREATE VIEW ledger_rows AS
        SELECT q.id, q.created_at, q.config_name, q.url, q.check_type, q.status, q.reason,
               q.model, q.reasoning_effort, q.filter_name, q.prompt_hash, q.company,
               q.job_title, {instructions}q.instructions_id,
               {input_content} AS input_content,
               q.parsed_json,
               {_usage("prompt_tokens", "0", stored)},
               {_usage("completion_tokens", "0", stored)},
               {_usage("total_tokens", "0", stored)},
               {_usage("cached_tokens", "0", stored)},
               {_usage("cache_write_tokens", "0", stored)},
               {_usage("reasoning_tokens", "0", stored)},
               {_usage("duration_ms", "NULL", stored)},
               q.error,
               {_usage("cost_usd", cost_sibling, stored)},
               q.worker, q.batch_id
        FROM ai_queries q
        UNION ALL
        SELECT id, created_at, 'content-cache', url, 'content', status, method,
               NULL, NULL, NULL, NULL, NULL, NULL,
               {fetch_instructions}NULL::bigint, content, NULL,
               NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint,
               NULL::bigint, NULL::bigint, NULL::bigint, NULL, NULL::numeric,
               worker, NULL
        FROM page_fetches
        WHERE method <> 'verification'
    """


def _verdicts(stored: bool) -> str:
    instructions = "instructions, " if stored else ""
    return f"""
        CREATE VIEW verdicts AS
        SELECT id, created_at, url, check_type, status, reason, model,
               reasoning_effort, filter_name, prompt_hash, company, job_title,
               {instructions}instructions_id, parsed_json, config_name, batch_id,
               worker, request_sha256
        FROM ai_queries
        WHERE check_type <> 'content'
          AND status IN ('passed', 'rejected')
    """


def upgrade() -> None:
    nulls = " AND ".join(f"{c} IS NULL" for c in _COPIES)
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '10s'")
        op.execute(f"ALTER TABLE ai_queries DROP CONSTRAINT IF EXISTS {_GUARD}")
        op.execute(f"ALTER TABLE ai_queries ADD CONSTRAINT {_GUARD} CHECK ({nulls}) NOT VALID")
        op.execute(f"ALTER TABLE ai_queries VALIDATE CONSTRAINT {_GUARD}")
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("DROP VIEW ledger_rows")
    op.execute("DROP VIEW verdicts")
    for column in _COPIES:
        op.drop_column("ai_queries", column)
    # Dropping a column drops a CHECK that names it; this is for clarity.
    op.execute(f"ALTER TABLE ai_queries DROP CONSTRAINT IF EXISTS {_GUARD}")
    op.execute(_verdicts(stored=False))
    op.execute(_ledger_rows(stored=False))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("DROP VIEW ledger_rows")
    op.execute("DROP VIEW verdicts")
    for column, type_ in _COPIES.items():
        op.add_column("ai_queries", sa.Column(column, type_, nullable=True))
    op.execute(_verdicts(stored=True))
    op.execute(_ledger_rows(stored=True))
