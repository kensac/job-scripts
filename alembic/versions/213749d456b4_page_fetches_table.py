"""page_fetches: the fetch table takes its name, the views lose their ai_queries arms

The move of page fetches out of ai_queries is finished. On production on
2026-10-10: 0 rows of ai_queries have check_type 'content', and 0 urls have
page text only as a copy on an answer (every such url has a fetch row with
text, 11,272 of them made from those copies by the move). So:

- page_fetch_rows becomes page_fetches, replacing the view of that name,
  which was the table plus the ai_queries content arm (now empty).
- page_texts is the fetched text alone: the copies on answers are no
  longer page text. On the 4,847 urls (of 839,949) where a copy was the
  newest text, it was the same text as the url's newest fetch on 4,846.
- ledger_rows keeps its columns, over the renamed table.

The migration refuses to run if either count is not 0, before it changes
anything. Both checks took 9 s on production.

Images still running when this applies name page_fetch_rows (the fetch
writer, the admin delete) and page_texts.on_verdict. A view page_fetch_rows
over the table takes their reads, inserts and deletes, and on_verdict stays
as a column that is always false. No code in this release names either; the
next release drops both.

Revision ID: 213749d456b4
Revises: c0acfe30a567
Create Date: 2026-10-10 18:10:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "213749d456b4"
down_revision: str | None = "c0acfe30a567"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = ("url_id", "created_at", "status", "worker_recent")

_PREFLIGHT = """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM ai_queries WHERE check_type = 'content') THEN
            RAISE EXCEPTION 'ai_queries still holds page fetches; the move has not finished';
        END IF;
        IF EXISTS (
            SELECT 1 FROM ai_queries a
            WHERE a.check_type NOT IN ('content', 'custom')
              AND a.input_content IS NOT NULL AND a.input_content <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM {table} r
                  WHERE r.url = a.url AND r.content IS NOT NULL AND r.content <> ''
              )
        ) THEN
            RAISE EXCEPTION 'a url has page text only as a copy on an answer';
        END IF;
    END $$
"""

_PAGE_TEXTS = """
    CREATE VIEW page_texts AS
    SELECT id, url, content AS input_content, created_at, false AS on_verdict
    FROM page_fetches WHERE content IS NOT NULL AND content <> ''
"""

# For the images still running when this applies: their fetch writer and
# admin delete name page_fetch_rows, and a plain view of one table is
# updatable, so their inserts and deletes reach the table. Dropped with
# page_texts.on_verdict by the next release, once no image names either.
_COMPAT = "CREATE VIEW page_fetch_rows AS SELECT * FROM page_fetches"

# The admin ledger, board spend and the derivation scopes read every row
# ai_queries holds plus every fetch, in ai_queries' shape. A fetch made from
# an answer's copy (method 'verification') has that answer's id and is
# already listed as the answer.
_LEDGER_ROWS = """
    CREATE VIEW ledger_rows AS
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
    FROM {table}
    WHERE method <> 'verification'
"""

_PAGE_FETCHES_BEFORE = """
    CREATE VIEW page_fetches AS
    SELECT id, url, status, reason AS method, input_content AS content, worker, created_at
    FROM ai_queries WHERE check_type = 'content'
    UNION ALL
    SELECT id, url, status, method, content, worker, created_at FROM page_fetch_rows
"""

_PAGE_TEXTS_BEFORE = """
    CREATE VIEW page_texts AS
    SELECT id, url, content AS input_content, created_at, false AS on_verdict
    FROM page_fetches WHERE content IS NOT NULL AND content <> ''
    UNION ALL
    SELECT a.id, a.url, a.input_content, a.created_at, true AS on_verdict
    FROM ai_queries a
    WHERE a.check_type NOT IN ('content', 'custom')
      AND a.input_content IS NOT NULL AND a.input_content <> ''
      AND NOT EXISTS (SELECT 1 FROM page_fetch_rows r WHERE r.id = a.id)
"""


def _rename(old: str, new: str) -> None:
    op.execute(f"ALTER TABLE {old} RENAME TO {new}")
    op.execute(f"ALTER INDEX {old}_pkey RENAME TO {new}_pkey")
    for name in _INDEXES:
        op.execute(f"ALTER INDEX idx_{old}_{name} RENAME TO idx_{new}_{name}")
    op.execute(f"ALTER TABLE {new} RENAME CONSTRAINT ck_{old}_status TO ck_{new}_status")


def upgrade() -> None:
    op.execute(_PREFLIGHT.format(table="page_fetch_rows"))
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("DROP VIEW ledger_rows")
    op.execute("DROP VIEW page_texts")
    op.execute("DROP VIEW page_fetches")
    _rename("page_fetch_rows", "page_fetches")
    op.execute(_COMPAT)
    op.execute(_PAGE_TEXTS)
    op.execute(_LEDGER_ROWS.format(table="page_fetches"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("DROP VIEW ledger_rows")
    op.execute("DROP VIEW page_texts")
    op.execute("DROP VIEW page_fetch_rows")
    _rename("page_fetches", "page_fetch_rows")
    op.execute(_PAGE_FETCHES_BEFORE)
    op.execute(_PAGE_TEXTS_BEFORE)
    op.execute(_LEDGER_ROWS.format(table="page_fetch_rows"))
