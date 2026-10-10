"""page_fetch_rows: page fetches get a table of their own

ai_queries holds 910,622 page fetches (check_type 'content', 2.6 GB of text
on 2026-10-09) beside the model's answers. A fetch is a fact about a page and
has none of an answer's columns: no model, tokens, cost or prompt. This adds
the table they move to and points readers at it without changing what they
see:

- page_fetches is the fetches in both places. Writers move to the table in
  this release; a task then moves the old rows, keeping their ids, and the
  view drops its ai_queries arm when none are left.
- ledger_rows is every row ai_queries held, fetches included, for the
  readers that count them all: the admin ledger, board spend, and the
  scopes of the derivation sweeps.
- page_texts keeps its columns and reads page_fetches for fetched text, plus
  the copies older verification wrote onto its answers. A copy the move has
  brought into the table is read from the table only.

Ids come from ai_queries_id_seq, so a moved fetch keeps its id and every
content_row_id that names it stays true. job_profiles' foreign key to
ai_queries is dropped because it cascades: moving a fetch would delete the
profiles read from it. Dropping it locks ai_queries briefly, under a lock
timeout so a long reader turns into a retried start rather than a queue of
blocked inserts.

Revision ID: 5460c0343629
Revises: b3e9f2a41c77
Create Date: 2026-10-09 23:52:45.935261

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5460c0343629"
down_revision: str | None = "b3e9f2a41c77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PAGE_FETCHES = """
    CREATE OR REPLACE VIEW page_fetches AS
    SELECT id, url, status, reason AS method, input_content AS content, worker, created_at
    FROM ai_queries WHERE check_type = 'content'
    UNION ALL
    SELECT id, url, status, method, content, worker, created_at FROM page_fetch_rows
"""

_PAGE_TEXTS = """
    CREATE OR REPLACE VIEW page_texts AS
    SELECT id, url, content AS input_content, created_at, false AS on_verdict
    FROM page_fetches WHERE content IS NOT NULL AND content <> ''
    UNION ALL
    SELECT a.id, a.url, a.input_content, a.created_at, true AS on_verdict
    FROM ai_queries a
    WHERE a.check_type NOT IN ('content', 'custom')
      AND a.input_content IS NOT NULL AND a.input_content <> ''
      AND NOT EXISTS (SELECT 1 FROM page_fetch_rows r WHERE r.id = a.id)
"""

# The admin ledger, board spend and the derivation scopes read every row
# ai_queries ever held, page fetches included. This keeps their answers the
# same while fetches move: a fetch is in exactly one arm at any moment,
# because the move deletes and inserts in one statement.
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

_PAGE_TEXTS_BEFORE = """
    CREATE OR REPLACE VIEW page_texts AS
    SELECT id, url, input_content, created_at, check_type <> 'content' AS on_verdict
    FROM ai_queries
    WHERE check_type <> 'custom' AND input_content IS NOT NULL AND input_content <> ''
"""


def upgrade() -> None:
    op.create_table(
        "page_fetch_rows",
        sa.Column(
            "id",
            sa.BigInteger(),
            server_default=sa.text("nextval('ai_queries_id_seq')"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("worker", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('passed', 'failed')", name="ck_page_fetch_rows_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_page_fetch_rows_created_at", "page_fetch_rows", ["created_at"], unique=False
    )
    op.create_index("idx_page_fetch_rows_status", "page_fetch_rows", ["status"], unique=False)
    op.create_index(
        "idx_page_fetch_rows_worker_recent",
        "page_fetch_rows",
        ["worker", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_page_fetch_rows_url_id",
        "page_fetch_rows",
        ["url", sa.literal_column("id DESC")],
        unique=False,
    )
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.drop_constraint(op.f("job_profiles_content_row_id_fkey"), "job_profiles", type_="foreignkey")
    op.execute(_PAGE_FETCHES)
    op.execute(_PAGE_TEXTS)
    op.execute(_LEDGER_ROWS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS ledger_rows")
    op.execute(_PAGE_TEXTS_BEFORE)
    op.execute("DROP VIEW IF EXISTS page_fetches")
    op.create_foreign_key(
        op.f("job_profiles_content_row_id_fkey"),
        "job_profiles",
        "ai_queries",
        ["content_row_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_index("idx_page_fetch_rows_worker_recent", table_name="page_fetch_rows")
    op.drop_index("idx_page_fetch_rows_status", table_name="page_fetch_rows")
    op.drop_index("idx_page_fetch_rows_url_id", table_name="page_fetch_rows")
    op.drop_index("idx_page_fetch_rows_created_at", table_name="page_fetch_rows")
    op.drop_table("page_fetch_rows")
