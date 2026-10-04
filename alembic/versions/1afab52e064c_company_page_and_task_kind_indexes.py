"""company page and task kind indexes

idx_jobs_company_key is lower(btrim(company)), the company page's key. Every
per-page read on that page (open share, repost, currency) filters on it and
was a seq scan of jobs whatever the page.

idx_ai_queries_latest_verdict gains created_at in its INCLUDE list. The
company page's open share reads every closed verdict of the page's postings
for status and created_at; with created_at outside the index each one is a
heap fetch, which is where production's nested-loop plan spent 5 s. The index
is rebuilt under a new name and renamed over the old one, so the board's
latest-verdict reads always have one of the two.

idx_tasks_kind is (kind, id DESC) INCLUDE (status). tasks had no index on
kind: the latest task of a kind walked the table backwards, the job profile
report found its receipts' tasks by a seq scan, and the queue summary's
GROUP BY kind, status read every payload-bearing heap page.

Measured on a synthetic catalog (606k jobs, 3.0M ai_queries, 224k tasks,
2026-10-04), production being unreadable from where this was written. The
open share in production's nested-loop plan: 3.4-5.5 s before, 1.0 s after,
now index-only. On a page of smaller names: 400 ms to 35 ms. The queue
summary: 27 ms to 17 ms, reading 1,430 index pages instead of 13,237 heap
pages.

All three are built CONCURRENTLY so writers continue. A build that died
midway leaves an INVALID index that IF NOT EXISTS would mistake for a
finished one, so an invalid leftover is dropped first. A concurrent build
waits for every transaction older than itself; the lock timeout turns a wait
that will not end into a failed start that retries, as in bd1e66f153c3 and
507fe2f38949. ALTER INDEX RENAME takes SHARE UPDATE EXCLUSIVE, which blocks
neither reads nor writes.

Revision ID: 1afab52e064c
Revises: 1043e0d541ff
Create Date: 2026-10-03 22:32:59.608054

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1afab52e064c"
down_revision: str | None = "1043e0d541ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERDICT = "idx_ai_queries_latest_verdict"
_VERDICT_ON = (
    "ai_queries (url, check_type, id DESC) INCLUDE ({include}) "
    "WHERE check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')"
)
_ADDED = (
    ("idx_jobs_company_key", "jobs (lower(btrim(company)))"),
    ("idx_tasks_kind", "tasks (kind, id DESC) INCLUDE (status)"),
)


def _build(name: str, definition: str) -> None:
    invalid = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND NOT i.indisvalid"
        ),
        {"name": name},
    )
    if invalid.first():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {definition}")


def _rebuild_verdict(include: str) -> None:
    """Replace the verdict index with one carrying `include`, unless the live
    one already does (a start that died after the rename)."""
    current = op.get_bind().execute(
        sa.text("SELECT pg_get_indexdef(to_regclass(:name)::oid) AS d"), {"name": _VERDICT}
    )
    definition = current.scalar() or ""
    if f"INCLUDE ({include})" in definition:
        return
    staged = f"{_VERDICT}_next"
    _build(staged, _VERDICT_ON.format(include=include))
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_VERDICT}")
    op.execute(f"ALTER INDEX {staged} RENAME TO {_VERDICT}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # bd1e66f153c3's judgment: a start can afford a minute for the
        # transactions in flight, and past that the wait is not ending.
        op.execute("SET lock_timeout = '60s'")
        for name, definition in _ADDED:
            _build(name, definition)
        _rebuild_verdict("status, created_at")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        _rebuild_verdict("status")
        for name, _ in _ADDED:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute("RESET lock_timeout")
