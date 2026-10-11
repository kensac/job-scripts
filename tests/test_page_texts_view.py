"""`page_texts` is the one statement of which rows hold page text."""

from __future__ import annotations

import pathlib

from api import db
from core import page_fetches, store

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

PAGE = "Posting text. " * 40


def test_page_text_is_the_fetches_that_brought_text_back():
    fetched = page_fetches.record("https://x/a", "passed", "scraped", PAGE)
    page_fetches.record("https://x/a", "failed", "fetch returned nothing")

    rows = db.query("SELECT id, url FROM page_texts ORDER BY url")
    assert rows == [{"id": fetched, "url": "https://x/a"}]


def test_the_text_lookups_are_index_only():
    """CONTENT_LATERAL's id lookup and the content backfill's gap check read
    idx_page_fetches_url_text alone. A predicate that stops matching the view
    or MIN_CONTENT_CHARS sends every lookup back to the TOASTed heap."""
    db.execute(
        "INSERT INTO page_fetches (url, status, method, content) "
        "SELECT 'https://x/' || i, 'passed', 'scraped', repeat('x', 300) "
        "FROM generate_series(1, 5000) i"
    )
    db.execute("VACUUM ANALYZE page_fetches")
    lookups = (
        "SELECT q.id FROM (SELECT 'https://x/a' AS url) j "
        + store.CONTENT_LATERAL.format(url="j.url", columns="q.id"),
        "SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM page_texts q WHERE q.url = 'https://x/a' "
        f"AND length(q.input_content) > {store.MIN_CONTENT_CHARS})",
    )
    with db.pool.connection() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        for sql in lookups:
            plan = "\n".join(r["QUERY PLAN"] for r in conn.execute("EXPLAIN " + sql).fetchall())
            assert "Index Only Scan using idx_page_fetches_url_text" in plan, plan
