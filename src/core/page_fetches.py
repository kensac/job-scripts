"""The one writer of a page fetch.

A fetch is a fact about a posting's page: when it was read, how, and what
came back. Readers use the page_fetches and page_texts views, not the table,
so the rows still stored in ai_queries read the same until they are moved.
"""

from __future__ import annotations

from core.pool import connection
from core.store import WORKER


def record(url: str, status: str, method: str, content: str | None = None) -> int:
    """Append one fetch. `status` is 'passed' when text came back, 'failed'
    when nothing usable did; `method` says how it was read or why not."""
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO page_fetch_rows (url, status, method, content, worker) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (url, status, method, content, WORKER),
        ).fetchone()
    assert row is not None
    return row["id"]
